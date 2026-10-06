#!/usr/bin/env python3
"""code_search.py — ricerca nel codebase ospitante via 3 livelli.

Level 0: ripgrep (git grep se rg manca) + smart ranking (count + filename boost + recency)
Level 1: ripgrep top-50 + LLM haiku rerank semantico
Level 2: vector search su Redis (Vector Sets) + embed provider

Auto-detect default level basato su:
  - has_vector_index → level 2
  - codebase < 5k LOC → level 0
  - altrimenti → level 1

Override esplicito via smart_level=N.

Stdlib + subprocess `rg` + lazy import code_db/embed_providers per level 2.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# ============================================================
# Filesystem detection
# ============================================================

def _project_root(start: Path = None) -> Optional[Path]:
    """Target esplicito, RAIDHO_ROOT, poi discovery dalla cwd."""
    explicit = start if start is not None else os.environ.get("RAIDHO_ROOT")
    if explicit is not None:
        root = Path(explicit).expanduser().resolve()
        return root if (root / ".raidhowiki" / "meta.yaml").is_file() else None
    cur = Path.cwd().resolve()
    for parent in [cur] + list(cur.parents):
        if (parent / ".raidhowiki" / "meta.yaml").is_file():
            return parent
    return None


def _has_vector_index(project_root: Path) -> bool:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import code_db
        return code_db.exists(project_root / ".raidhowiki")
    except Exception:
        return False


_LOC_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
                   ".kt", ".rb", ".php", ".c", ".cpp"}
_LOC_SKIP_DIRS = {"node_modules", ".git", "__pycache__", "dist"}


def _quick_loc_count(project_root: Path, budget_sec: float = 5.0) -> int:
    """Conta LOC totali per linguaggi rilevanti. Nessuna shell (il path del progetto
    può contenere spazi/apici). Budget 5s: oltre, ritorna il parziale."""
    import time as _time
    deadline = _time.monotonic() + budget_sec
    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(project_root):
            dirnames[:] = [d for d in dirnames if d not in _LOC_SKIP_DIRS]
            for fn in filenames:
                if os.path.splitext(fn)[1] not in _LOC_EXTENSIONS:
                    continue
                try:
                    with open(os.path.join(dirpath, fn), "rb") as fh:
                        total += sum(1 for _ in fh)
                except OSError:
                    continue
            if _time.monotonic() > deadline:
                break
    except Exception:
        return total
    return total


def infer_default_level(project_root: Path) -> int:
    """Auto-detect smart_level basato su contesto."""
    if _has_vector_index(project_root):
        return 2
    loc = _quick_loc_count(project_root)
    if loc < 5000:
        return 0
    return 1


# ============================================================
# Level 0: ripgrep + smart ranking
# ============================================================

EXCLUDE_GLOBS = [
    "node_modules/**", "vendor/**", "dist/**", "build/**", "out/**",
    ".git/**", "__pycache__/**", "*.min.js", "*.min.css",
    ".raidhowiki/code-index.db",
]


def _ripgrep_search(query: str, root: Path, limit: int = 50, lang: Optional[str] = None) -> list[dict]:
    """ripgrep --json output → list di match strutturati."""
    cmd = ["rg", "--json", "--smart-case", "--max-count=20", "-C", "1", query, str(root)]
    for glob in EXCLUDE_GLOBS:
        cmd.extend(["--glob", f"!{glob}"])
    if lang:
        cmd.extend(["--type", lang])

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15, text=True)
    except FileNotFoundError:
        return []
    except subprocess.TimeoutExpired:
        return []

    matches = []
    for line in result.stdout.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") != "match":
            continue
        data = ev["data"]
        path = data["path"]["text"]
        line_no = data["line_number"]
        text = data["lines"]["text"].rstrip("\n")
        matches.append({"path": path, "line": line_no, "text": text})
        if len(matches) >= limit:
            break
    return matches


def _git_grep_search(query: str, root: Path, limit: int = 50, lang: Optional[str] = None) -> list[dict]:
    """Ripiego senza ripgrep: `git grep --no-index` rispetta .gitignore (anche fuori da un repo)
    e, come rg, salta file e cartelle nascosti, quindi niente segreti gitignored nei risultati."""
    cmd = ["git", "-C", str(root), "grep", "--no-index", "--exclude-standard", "-z", "-n", "-I", "-E"]
    if query == query.lower():
        cmd.append("-i")  # come --smart-case di rg
    cmd += ["-e", query, "--"]
    if lang:
        from code_index import LANG_BY_EXT
        cmd += [f":(glob)**/*{ext}" for ext, name in LANG_BY_EXT.items() if name == lang] or [f":(glob)**/*.{lang}"]
    else:
        cmd.append(".")
    cmd += [f":(exclude,glob){g}" for g in ("**/.*", "**/.*/**", *(f"**/{g}" for g in EXCLUDE_GLOBS))]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15, text=True, errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    matches = []
    per_file: dict[str, int] = {}
    for line in result.stdout.split("\n"):
        try:
            path, line_no, text = line.split("\0", 2)
            line_no = int(line_no)
        except ValueError:
            continue
        if per_file.get(path, 0) >= 20:  # come --max-count=20 di rg
            continue
        per_file[path] = per_file.get(path, 0) + 1
        matches.append({"path": str(root / path), "line": line_no, "text": text})
        if len(matches) >= limit:
            break
    return matches


def _git_recency(path: str, root: Path) -> float:
    """Boost basato su recency del file via git log. Restituisce [0, 1]."""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(root), "log", "-1", "--format=%ct", "--", path],
            stderr=subprocess.DEVNULL, timeout=3,
        ).decode().strip()
        if not out:
            return 0.0
        commit_ts = int(out)
        days_ago = (datetime.now().timestamp() - commit_ts) / 86400
        # Decay esponenziale: 1.0 oggi, 0.5 a 30 giorni, 0.1 a 100 giorni
        return max(0.0, 1.0 / (1.0 + days_ago / 30))
    except Exception:
        return 0.0


def _smart_rank(matches: list[dict], query: str, root: Path) -> list[dict]:
    """Ranking: count_in_file * 1.0 + filename_match * 2.0 + recency * 1.5."""
    q_lower = query.lower()
    by_file: dict[str, dict] = {}

    for m in matches:
        path = m["path"]
        if path not in by_file:
            by_file[path] = {"path": path, "matches": [], "score": 0.0}
        by_file[path]["matches"].append(m)

    for path, entry in by_file.items():
        n_matches = len(entry["matches"])
        score = float(n_matches)

        filename = Path(path).name.lower()
        if q_lower in filename:
            score += 2.0

        # Boost se query match in def/class lines
        for m in entry["matches"]:
            text = m["text"].lower()
            if any(kw in text for kw in (f"def {q_lower}", f"class {q_lower}", f"function {q_lower}", f"func {q_lower}")):
                score += 3.0
                break

        # Recency
        score += _git_recency(path, root) * 1.5

        entry["score"] = score

    return sorted(by_file.values(), key=lambda e: -e["score"])


def search_level_0(query: str, root: Path, limit: int = 20, lang: Optional[str] = None) -> dict:
    """ripgrep (o git grep se rg manca) + smart ranking. Restituisce top file con match preview."""
    rg = shutil.which("rg") is not None
    raw_matches = (_ripgrep_search if rg else _git_grep_search)(query, root, limit=200, lang=lang)
    ranked = _smart_rank(raw_matches, query, root)

    results = []
    for entry in ranked[:limit]:
        rel_path = str(Path(entry["path"]).relative_to(root)) if Path(entry["path"]).is_absolute() else entry["path"]
        results.append({
            "path": rel_path,
            "score": round(entry["score"], 2),
            "match_count": len(entry["matches"]),
            "preview": entry["matches"][:3],  # top 3 match per file
        })
    if rg:
        return {"level": 0, "method": "ripgrep_smart_rank", "results": results, "count": len(results)}
    return {"level": 0, "method": "git_grep_smart_rank", "results": results, "count": len(results),
            "_ripgrep_missing": "ripgrep (rg) not installed: literal search done with git grep"}


# ============================================================
# Level 1: ripgrep + LLM haiku rerank
# ============================================================

def search_level_1(query: str, root: Path, limit: int = 10, lang: Optional[str] = None) -> dict:
    """Level 0 top 50 → spawn claude haiku per rerank semantico."""
    level0 = search_level_0(query, root, limit=50, lang=lang)
    import index_policy
    model = os.environ.get("RAIDHO_SEARCH_RERANK_MODEL", "haiku")
    try:
        if index_policy.config(root).get("rerank_model") != model:
            level0["_fallback_reason"] = "rerank model not authorized in index-policy.json"
            level0["results"] = level0["results"][:limit]
            level0["count"] = len(level0["results"])
            return level0
        allowed = {str(path.relative_to(root)) for path in index_policy.discover(root)[0]}
        level0["results"] = [r for r in level0["results"] if r["path"] in allowed]
    except Exception:
        return {"error": "cannot evaluate outbound data policy", "code": "index_policy_error"}
    if not level0["results"]:
        return {"level": 1, "method": "ripgrep_llm_rerank", "results": [], "count": 0, "note": "no ripgrep matches"}

    # Build prompt
    candidates_text = []
    for i, r in enumerate(level0["results"]):
        previews = "\n".join(f"  L{m['line']}: {m['text'][:120]}" for m in r["preview"])
        candidates_text.append(f"[{i}] {r['path']}\n{previews}")

    prompt = (
        f"Query: {query}\n\n"
        f"Sono stati trovati {len(level0['results'])} file candidati via ricerca keyword. "
        f"Per ogni file vedi: path + 3 righe più rilevanti (con numero L). "
        f"Devi RANKARE i top {limit} per relevance semantica alla query (non solo keyword).\n\n"
        f"OUTPUT FORMAT: solo lista di indici [0..N-1] ordinati per relevance descending, "
        f"max {limit} indici, separati da virgola. NIENTE preambolo, NIENTE spiegazioni.\n\n"
        f"Esempio output: 3,7,1,12,0,5,9,2,8,15\n\n"
        f"--- CANDIDATI ---\n" + "\n\n".join(candidates_text)
    )

    claude_bin = os.environ.get("RAIDHO_CLAUDE_BIN", "claude")
    model = os.environ.get("RAIDHO_SEARCH_RERANK_MODEL", "haiku")
    # Sessione-macchina: niente journal/summary/embed dalla sub-sessione di rerank.
    child_env = dict(os.environ, RAIDHO_JOURNAL="0", RAIDHO_AUTO_SUMMARY="0", RAIDHO_WIKI_EMBED="0")
    try:
        result = subprocess.run(
            [claude_bin, "-p", prompt, "--model", model],
            capture_output=True, timeout=90, text=True, env=child_env, stdin=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        # Fallback level 0 se claude non disponibile
        return {
            "level": 0,
            "method": "ripgrep_smart_rank",
            "results": level0["results"][:limit],
            "count": min(len(level0["results"]), limit),
            "_fallback_reason": f"LLM rerank failed: {type(e).__name__}",
        }

    if result.returncode != 0:
        return {
            "level": 0,
            "method": "ripgrep_smart_rank",
            "results": level0["results"][:limit],
            "count": min(len(level0["results"]), limit),
            "_fallback_reason": f"claude rc={result.returncode}",
        }

    # Parse indices
    out = result.stdout.strip()
    indices = []
    for tok in re.split(r"[,\s]+", out):
        try:
            idx = int(tok)
            if 0 <= idx < len(level0["results"]):
                indices.append(idx)
        except ValueError:
            continue

    if not indices:
        return {
            "level": 0,
            "method": "ripgrep_smart_rank",
            "results": level0["results"][:limit],
            "count": min(len(level0["results"]), limit),
            "_fallback_reason": "LLM returned no parsable indices",
        }

    reranked = [level0["results"][i] for i in indices[:limit]]
    return {
        "level": 1,
        "method": "ripgrep_llm_rerank",
        "model": model,
        "results": reranked,
        "count": len(reranked),
    }


# ============================================================
# Level 2: vector search
# ============================================================

def search_level_2(query: str, project_root: Path, limit: int = 10, lang: Optional[str] = None) -> dict:
    """Vector search su Redis. Embed query con stesso provider usato per index."""
    # Lazy imports
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import code_db
        import embed_providers
    except ImportError as e:
        return {"level": 1, "results": [], "_fallback_reason": f"missing module: {e}"}

    raidhowiki = project_root / ".raidhowiki"
    if not code_db.exists(raidhowiki):
        # Fallback lessicale senza chiamate remote.
        l1 = search_level_0(query, project_root, limit=limit, lang=lang)
        l1["_fallback_reason"] = "vector index not built. Run `code.reindex` or `/raidho-index-code`."
        return l1

    try:
        provider = embed_providers.get_project_provider(project_root)
    except ValueError as exc:
        result = search_level_0(query, project_root, limit=limit, lang=lang)
        result["_fallback_reason"] = str(exc)
        result["code"] = "index_policy_error"
        return result
    if provider is None:
        l1 = search_level_0(query, project_root, limit=limit, lang=lang)
        l1["_fallback_reason"] = "no embed provider available"
        return l1

    from index_pipeline import index_status
    status = index_status(project_root, provider)
    # un indice indietro rispetto al codice resta utile (search_evidence scarta i risultati
    # dei file cambiati): si ripiega su ripgrep solo senza un'indicizzazione riuscita
    usabile = status["status"] == "ready" or (
        status["status"] in ("stale", "partial", "building", "failed")
        and (status.get("scopes") or {}).get("code", {}).get("last_success"))
    if not usabile:
        result = search_level_0(query, project_root, limit=limit, lang=lang)
        result["_fallback_reason"] = "vector index " + status["status"]
        result["index_status"] = status["status"]
        return result
    db = None
    try:
        query_vecs = provider.embed([query])
        code_db.validate_vectors(query_vecs, 1, provider.dim)
        db = code_db.open_db(raidhowiki, dim=provider.dim, create_if_missing=False, provider=provider)
        hits = code_db.vector_search(db, query_vecs[0], limit=limit, lang_filter=lang, kind_filter="code")
        # la revisione del file e' nel chunk stesso: coerente col vettore restituito
        for hit in hits:
            hit['_indexed_revision'] = hit.get('rev')
    except Exception as e:
        result = search_level_0(query, project_root, limit=limit, lang=lang)
        result["_fallback_reason"] = f"vector search failed: {e}"
        return result
    finally:
        if db is not None:
            db.close()

    results = []
    for h in hits:
        results.append({
            "path": h["file_path"],
            "func_name": h["func_name"],
            "line_start": h["line_start"],
            "line_end": h["line_end"],
            # pezzo di una riga oltre il tetto dei chunk: colonne 0-based, col_end escluso
            **({"col_start": h["col_start"], "col_end": h["col_end"]} if h.get("col_start") is not None else {}),
            "lang": h["lang"],
            "distance": round(h["distance"], 4),
            "preview": h["content"][:300],
            "_indexed_revision": h["_indexed_revision"],
            "_indexed_content": h["content"],
        })

    return {
        "level": 2,
        "method": "vector_search",
        "provider": provider.name,
        "model": provider.model,
        "results": results,
        "count": len(results),
        "index_status": status["status"],
        **({"_note": "index behind the code: results from changed files may be outdated"}
           if status["status"] != "ready" else {}),
    }


# ============================================================
# Entry point
# ============================================================

def code_search(query: str, smart_level: Optional[int] = None, limit: int = 10, lang: Optional[str] = None, root: Optional[Path] = None, max_preview_chars: int = 12000) -> dict:
    """Entry point: auto-detect default level + dispatch."""
    if not query.strip():
        return {"error": "query required"}

    if type(limit) is not int or not 1 <= limit <= 50:
        return {"error": "limit must be an integer between 1 and 50", "code": "invalid_search_options"}
    if type(max_preview_chars) is not int or not 0 <= max_preview_chars <= 100000:
        return {"error": "max_preview_chars must be an integer between 0 and 100000", "code": "invalid_search_options"}
    root = _project_root(root)
    if root is None:
        return {"error": "not in an raidho project (no `.raidhowiki/meta.yaml` found in parent dirs)"}

    if smart_level is None:
        smart_level = infer_default_level(root)
    smart_level = max(0, min(2, int(smart_level)))

    if smart_level == 0:
        result = search_level_0(query, root, limit=limit, lang=lang)
    elif smart_level == 1:
        result = search_level_1(query, root, limit=limit, lang=lang)
    else:
        result = search_level_2(query, root, limit=limit, lang=lang)
    from search_evidence import finalize
    return finalize(result, root, query, max_preview_chars)
