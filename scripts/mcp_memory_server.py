#!/usr/bin/env python3
"""
mcp_memory_server.py — entry point del MCP server "raidho_memory" (package scripts/raidho/).

Implementa JSON-RPC 2.0 over stdio (spec MCP 2025-03-26 / 2024-11-05 compat).
Esposto a Claude Code, OpenCode, e qualsiasi altro MCP host via .mcp.json:

    {
      "mcpServers": {
        "raidho_memory": {
          "command": "python3",
          "args": ["/abs/path/to/mcp_memory_server.py"],
          "env": {
            "RAIDHO_SCOPE": "project",                    // o "hub"
            "RAIDHO_ROOT": "/abs/path/to/project-root"    // o hub-root
          }
        }
      }
    }

Tool esposti: 9 gruppi core (memory, sessions, soul, user, skills, wiki, roadmap,
code, graph). Vedi TOOL_GROUPS per l'elenco autoritativo e RAIDHO_TOOL_GROUPS env
per il filtraggio runtime (default: tutti i core).

Dal v0.21 i gruppi agents/tasks/workspace/kanban/goals/pp non fanno piu' parte
del plugin: questo server e' un plugin CLI puro.

Tool storicamente "futuri" non implementati (parking):
    - sessions.spawn      — crea nuova session per agent (mai necessitato)
    - memory.summarize    — aggregate cross-session summaries (post auto-summary)

Auto-summary per singola sessione: vedi `sessions.summarize` (spawn claude CLI
subprocess on-demand, sostituisce placeholder nella sezione `## Summary`).

Stdlib pure, no deps esterne.
"""

import sys
from pathlib import Path

# Eseguito come script (`python3 scripts/mcp_memory_server.py`): il package è accanto a questo file.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from raidho import server as _server  # noqa: E402
from raidho.config import PROTO_VERSION, ROOT, SCOPE, SERVER_NAME, SERVER_VERSION  # noqa: E402, F401
from raidho.server import (  # noqa: E402, F401
    _HANDLER_OVERRIDES,
    _HUB_GROUPS_MOVED,
    GROUP_ORDER,
    MODULE_ORDER,
    TOOL_GROUPS,
    TOOL_HANDLERS,
    TOOLS,
    _build_registry,
    _canonical_name,
    _wire_name,
    handle_request,
    main,
)

_server.reset_env_cache()

# Compatibilità: chi carica questo file come modulo (steward.py, script esterni) trova
# ancora handler (`tool_*`) e helper allo stesso nome di prima dello split in package (v0.26).
from raidho import common as _common  # noqa: E402
from raidho import config as _config  # noqa: E402

for _m in (_config, _common) + MODULE_ORDER:
    for _k, _v in vars(_m).items():
        if not _k.startswith("__") and _k not in globals() and getattr(_v, "__module__", _m.__name__) == _m.__name__:
            globals()[_k] = _v
del _m, _k, _v

if __name__ == "__main__":
    main()
