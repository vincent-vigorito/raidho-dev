"""Test mirati per RAI-618 (T1+T2):
(1) la configurazione di ~/.raidho/embed/<id>.env viene caricata da raidho.config
    e un eventuale errore di caricamento non e' piu' silenzioso (WARN su stderr).
(2) senza ripgrep (rg) il livello 0 di code_search ripiega su git grep e riporta
    il motivo nel risultato (_ripgrep_missing).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import code_search  # noqa: E402


def test_config_loads_managed_secrets_env(tmp_path):
    """T1: ~/.raidho/embed/<id>.env viene caricato e imposta l'ambiente."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    fake_home = tmp_path / "home"
    embed_dir = fake_home / ".raidho" / "embed"
    embed_dir.mkdir(parents=True)

    ident = hashlib.sha1(str(project_root.resolve()).encode()).hexdigest()[:12]
    env_file = embed_dir / f"{ident}.env"
    env_file.write_text("RAIDHO_EMBED_PROVIDER=fastembed\nRAIDHO_EMBED_MODEL=test-model\n")

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["RAIDHO_ROOT"] = str(project_root)
    env["PYTHONPATH"] = str(SCRIPTS_DIR)

    proc = subprocess.run([
        sys.executable, "-c",
        "import raidho.config as cfg\n"
        "import os\n"
        "assert cfg._SECRETS_LOADED >= 1, f'Expected loaded > 0, got {cfg._SECRETS_LOADED}'\n"
        "assert os.environ.get('RAIDHO_EMBED_PROVIDER') == 'fastembed'\n"
        "assert os.environ.get('RAIDHO_EMBED_MODEL') == 'test-model'\n"
    ], env=env, capture_output=True, text=True)

    assert proc.returncode == 0, f"Process failed: stdout={proc.stdout}, stderr={proc.stderr}"


def test_config_load_secrets_failure_warns_on_stderr(tmp_path):
    """T1: errore nel caricamento dei segreti viene segnalato su stderr e non blocca l'import."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["RAIDHO_ROOT"] = str(project_root)
    env["PYTHONPATH"] = str(SCRIPTS_DIR)

    proc = subprocess.run([
        sys.executable, "-c",
        "from unittest.mock import patch\n"
        "import importlib.util\n"
        "def failing_spec(*args, **kwargs):\n"
        "    raise RuntimeError('simulated loader failure')\n"
        "with patch('importlib.util.spec_from_file_location', side_effect=failing_spec):\n"
        "    import raidho.config as cfg\n"
        "    assert cfg._SECRETS_LOADED == 0\n"
    ], env=env, capture_output=True, text=True)

    assert proc.returncode == 0, f"Process crashed: stdout={proc.stdout}, stderr={proc.stderr}"
    assert "[raidho_memory] WARN secrets non caricati" in proc.stderr
    assert "simulated loader failure" in proc.stderr


def test_search_level_0_fallback_without_ripgrep(tmp_path):
    """T2: senza ripgrep il livello 0 usa git grep con smart rank e riporta _ripgrep_missing."""
    (tmp_path / "calculator.py").write_text("def compute_total():\n    return 100\n")
    hidden_dir = tmp_path / ".hidden"
    hidden_dir.mkdir()
    (hidden_dir / "secret.py").write_text("def compute_total():\n    pass\n")

    with patch("shutil.which", return_value=None):
        res = code_search.search_level_0("compute_total", tmp_path)

    assert res["level"] == 0
    assert res["method"] == "git_grep_smart_rank"
    assert "_ripgrep_missing" in res
    assert "ripgrep (rg) not installed" in res["_ripgrep_missing"]
    assert res["count"] == 1
    assert res["results"][0]["path"] == "calculator.py"
    assert any("compute_total" in p["text"] for p in res["results"][0]["preview"])
