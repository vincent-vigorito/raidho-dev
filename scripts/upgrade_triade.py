#!/usr/bin/env python3
"""
upgrade_triade.py — backfill triade AGENTS+SOUL+TOOLS in un progetto/hub esistente.

Per progetti pre-Fase 8 che hanno già `.raidhowiki/` ma non hanno la triade.
Aggiunge senza distruggere:
  - AGENTS.src.md, SOUL.md, TOOLS.md source (skip se esistono già)
  - AGENTS.md (composed cross-harness) + CLAUDE.md (@AGENTS.md wrapper) via compose
  - .mcp.json con raidho_memory registrato (merge non-destructive)
  - .raidhowiki/config.json (project) o config.json (hub) con `memory:` section
  - TOOLS.md auto-generato

Detection automatica project-vs-hub:
  - Se `<target>/.raidhowiki/meta.yaml` esiste → project
  - Altrimenti se `<target>/config/projects.json` esiste → hub
  - Altrimenti errore

Usage:
    python3 upgrade_triade.py --target /path/to/project-or-hub
    python3 upgrade_triade.py --target ... --type dev|research|business|personal|hub  # override
    python3 upgrade_triade.py --target ... --dry-run
"""

import argparse
import importlib.util
import sys
from datetime import date
from pathlib import Path


def detect_kind(target: Path) -> str:
    if (target / ".raidhowiki" / "meta.yaml").is_file():
        return "project"
    if (target / "config" / "projects.json").is_file():
        return "hub"
    return "unknown"


def detect_type(target: Path, kind: str) -> str:
    if kind == "hub":
        return "hub"
    # project: read meta.yaml type field (semplice grep, no yaml parser)
    meta = target / ".raidhowiki" / "meta.yaml"
    if meta.is_file():
        for line in meta.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("type:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    return "dev"


def get_plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def upgrade_project(target: Path, project_type: str, dry_run: bool = False) -> int:
    plugin = get_plugin_root()
    init_script = plugin / "scripts" / "init_project.py"
    spec = importlib.util.spec_from_file_location("init_project", str(init_script))
    ip = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ip)

    name = target.name
    created = date.today().isoformat()
    soul_baseline = ip.get_soul_baseline(project_type)
    triade_replacements = {
        "DATE": created,
        "PROJECT_NAME": name,
        "PROJECT_DESCRIPTION": f"Progetto {project_type} raidho-managed",
        "PROJECT_TYPE": project_type,
        "PROJECT_TYPE_DESCRIPTION": ip._type_description(project_type),
        "SOUL_BASELINE": soul_baseline,
        "USER_NAME": ip._detect_user_name(),
        "USER_LANG": "it",
        "USER_TONE": "diretto e conciso",
    }

    print(f"[upgrade_triade] target={target} kind=project type={project_type}")
    if dry_run:
        print("[dry-run] would write triade + symlink + MCP register + config + TOOLS.md + schema-version")
        return 0

    from project_recovery import snapshot_project
    snapshot_project(target, reason="schema_upgrade")
    ip.write_triade(target, triade_replacements)
    ip.make_claude_md_symlink(target)
    # config.json va in <target>/.raidhowiki/ per progetti
    ip._write_config_json(target / ".raidhowiki")
    ip._write_schema_version(target / ".raidhowiki")
    # Fase P-Plugin — force_update_env per backfill RAIDHO_TOOL_GROUPS su progetti vecchi
    ip._register_raidho_memory_mcp(target, force_update_env=True)
    ip._regenerate_tools_md(target)
    ip._compose_claude_md(target)
    return 0


def upgrade_hub(target: Path, dry_run: bool = False) -> int:
    print(f"[upgrade_triade] {target}: l'upgrade di un hub non e' supportato dal plugin", file=sys.stderr)
    return 2


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--target", required=True, help="project root or hub root")
    p.add_argument("--type", default=None, choices=["dev", "research", "business", "personal", "automation", "hub"],
                   help="override type detection")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    target = Path(args.target).expanduser().resolve()
    if not target.is_dir():
        sys.exit(f"ERROR: target not found: {target}")

    kind = "hub" if args.type == "hub" else detect_kind(target)
    if kind == "unknown":
        sys.exit(f"ERROR: target {target} non sembra né un progetto raidho (manca .raidhowiki/meta.yaml) né un hub (manca config/projects.json)")

    project_type = args.type if args.type else detect_type(target, kind)

    if kind == "hub":
        sys.exit(upgrade_hub(target, dry_run=args.dry_run))
    else:
        sys.exit(upgrade_project(target, project_type, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
