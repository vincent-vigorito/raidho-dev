"""Configurazione del server raidho_memory: env (RAIDHO_SCOPE/RAIDHO_ROOT), versione, secrets, path del plugin."""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROTO_VERSION = "2024-11-05"


SERVER_NAME = "raidho_memory"


SERVER_VERSION = "0.32.0"


SCOPE = os.environ.get("RAIDHO_SCOPE", "project")  # project | hub | agent


ROOT = Path(os.environ.get("RAIDHO_ROOT", os.getcwd())).resolve()


# Cartelle del plugin: `scripts/` (moduli CLI condivisi: code_db, wiki_embed, ...) e la root.
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = SCRIPTS_DIR.parent


def _load_secrets_env() -> int:
    """Auto-load `.secrets.env` via secrets_loader (modulo condiviso con CLI scripts)."""
    try:
        import importlib.util
        sp = SCRIPTS_DIR / "secrets_loader.py"
        spec = importlib.util.spec_from_file_location("secrets_loader", sp)
        sl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sl)
        return sl.load_secrets(ROOT, scope=SCOPE)
    except Exception as exc:
        print(f"[raidho_memory] WARN secrets non caricati per {ROOT}: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return 0


_SECRETS_LOADED = _load_secrets_env()


# Logging diagnostico opt-in: RAIDHO_LOG=debug → ogni eccezione recuperata in
# silenzio viene tracciata su stderr (stdout è riservato al JSON-RPC). Default: solo warn.
LOG_LEVEL = (os.environ.get("RAIDHO_LOG") or "").strip().lower()


def log(msg: str, level: str = "debug") -> None:
    if level == "warn" or LOG_LEVEL == "debug":
        print(f"[raidho_memory] {level.upper()} {msg}", file=sys.stderr, flush=True)


def log_exc(where: str, exc: BaseException) -> None:
    """Traccia un'eccezione gestita best-effort (visibile solo con RAIDHO_LOG=debug)."""
    if LOG_LEVEL == "debug":
        print(f"[raidho_memory] DEBUG {where}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
