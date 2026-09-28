#!/usr/bin/env python3
"""secrets_loader.py — autoload `.secrets.env` dello scope nell'env del processo.

Pattern dotenv minimale (KEY=VALUE per riga, supporta quoted values + commenti #).
Priorità: env shell esistente prevale (`os.environ.setdefault`).

Riusato sia dal MCP server (boot) sia dagli script CLI standalone (wiki_embed,
graph_report, graph_html, code_index). Senza questo, gli script falliscono
con "no embed provider" anche se la chiave è in `.secrets.env`.

Locations cercate per scope:
  project: <root>/.raidhowiki/.secrets.env  → fallback  <root>/.secrets.env
  hub:     <root>/.secrets.env
  sempre, per ultimo: ~/.raidho/secrets.env dell'utente (fuori dal repo e da ~/.raidhodev,
  che il provisioning riscrive)

Un valore rimasto `${NOME}` (placeholder di .mcp.json non espanso: l'agente lancia
le CLI con un ambiente ridotto) conta come assente e si prende dal file.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def segnaposto(valore: str) -> bool:
    """`${NOME}` non espanso: non e' un valore."""
    v = (valore or "").strip()
    return v.startswith("${") and v.endswith("}")


def managed_path(root: Path) -> Path:
    """Il file scritto da Raidho (Project settings → Semantic search) per questo progetto:
    provider, modello, dimensioni e chiave degli embedding, fuori dal repo."""
    ident = hashlib.sha1(str(Path(root).resolve()).encode()).hexdigest()[:12]
    return Path.home() / ".raidho" / "embed" / f"{ident}.env"


def _carica(path: Path, forza: bool = False) -> int:
    loaded = 0
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            if k and (forza or k not in os.environ or segnaposto(os.environ[k])):
                os.environ[k] = v
                loaded += 1
    except Exception:
        return loaded
    return loaded


def load_secrets(root: Path, scope: str = "project") -> int:
    """Carica `.secrets.env` per lo scope dato. Ritorna count variabili caricate.
    Idempotente: keys già in env vengono saltate (no override), tranne il file gestito
    da Raidho, che vince anche su .mcp.json: e' la scelta fatta nel progetto.
    """
    root = Path(root).resolve()
    loaded = _carica(managed_path(root), forza=True) if managed_path(root).is_file() else 0
    candidates: list[Path] = []
    if scope == "project":
        candidates.append(root / ".raidhowiki" / ".secrets.env")
        candidates.append(root / ".secrets.env")
    else:
        candidates.append(root / ".secrets.env")
    candidates.append(Path.home() / ".raidho" / "secrets.env")

    for path in candidates:
        if not path.is_file():
            continue
        n = _carica(path)
        loaded += n
        if n > 0:
            break  # first non-empty file wins
    return loaded


def autoload_from_env() -> int:
    """Convenience: auto-detect scope/root via env e carica.

    Usato dagli script CLI dove SCOPE/ROOT sono in env (ereditati dal MCP server)
    OR dove `RAIDHO_ROOT` è settato esplicitamente sulla CLI.
    """
    scope = os.environ.get("RAIDHO_SCOPE", "project")
    root = Path(os.environ.get("RAIDHO_ROOT", os.getcwd()))
    return load_secrets(root, scope=scope)
