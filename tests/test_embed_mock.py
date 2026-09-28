#!/usr/bin/env python3
"""Pipeline embedding end-to-end con provider deterministico (RAIDHO_EMBED_PROVIDER=mock):
wiki.embed → find_duplicates / search_semantic → code.reindex / code.search L2 →
graph.semantic_neighbors / graph.report / graph.html. Nessuna rete.

Richiede Redis 8 con i Vector Sets (RAIDHO_VECTOR_REDIS, default db 15 per i test) e il
modulo redis: altrimenti il test è SKIP. Override interprete: RAIDHO_TEST_PYTHON."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _helpers import cov_env

PLUGIN = Path(__file__).resolve().parents[1]
SERVER = PLUGIN / "scripts" / "mcp_memory_server.py"
INIT = PLUGIN / "scripts" / "init_project.py"
PY = os.environ.get("RAIDHO_TEST_PYTHON") or sys.executable
PASS = FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label} {detail}")


TEST_REDIS = os.environ.get("RAIDHO_VECTOR_REDIS", "redis://127.0.0.1:6379/15")


def redis_usable() -> bool:
    probe = (f"import sys, os; os.environ['RAIDHO_VECTOR_REDIS'] = {TEST_REDIS!r}; sys.path.insert(0, {str(PLUGIN / 'scripts')!r}); "
             "import code_db; print('ok' if code_db.redis_ok() else 'no')")
    r = subprocess.run([PY, "-c", probe], capture_output=True, text=True, timeout=30)
    return r.returncode == 0 and "ok" in r.stdout


def rpc(project: Path, calls: list[tuple[str, dict]]) -> list[dict]:
    env = {"RAIDHO_SCOPE": "project", "RAIDHO_ROOT": str(project), "RAIDHO_EMBED_PROVIDER": "mock", "RAIDHO_VECTOR_REDIS": TEST_REDIS, "RAIDHO_WIKI_EMBED": "0",
           "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(project.parent), **cov_env()}
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
    for i, (name, args) in enumerate(calls, start=2):
        msgs.append({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}})
    r = subprocess.run([PY, str(SERVER)], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                       capture_output=True, text=True, env=env, timeout=180)
    by_id = {}
    for line in r.stdout.splitlines():
        if line.startswith("{"):
            d = json.loads(line)
            by_id[d.get("id")] = d
    out = []
    for i in range(len(calls)):
        resp = by_id.get(i + 2) or {}
        if "error" in resp:
            out.append({"error": f"jsonrpc: {resp['error']}"})
            continue
        content = (resp.get("result") or {}).get("content") or [{}]
        try:
            out.append(json.loads(content[0].get("text", "{}")))
        except Exception:
            out.append({"error": f"non JSON: {resp}"})
    return out


def main() -> None:
    if not redis_usable():
        print(f"SKIP: Redis con Vector Sets non raggiungibile ({TEST_REDIS})")
        return
    tmp = Path(tempfile.mkdtemp(prefix="raidho-embed-"))
    project = tmp / "proj"
    project.mkdir()
    r = subprocess.run([PY, str(INIT), "--type", "dev", "--mode", "cold", "--target", str(project / ".raidhowiki"),
                        "--name", "proj"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    (project / "auth.py").write_text(
        "def authenticate_user(username, password):\n    '''login: verify password and issue session token'''\n"
        "    token = issue_session_token(username)\n    return token\n\n"
        "def issue_session_token(username):\n    return 'tok-' + username\n", encoding="utf-8")
    (project / "billing.py").write_text(
        "def compute_invoice_total(items):\n    '''billing: sum invoice line prices with tax'''\n"
        "    return sum(i['price'] for i in items) * 1.22\n", encoding="utf-8")

    # Score = similarità coseno (v0.27, metrica cosine in sqlite-vec). Col mock bag-of-words le
    # pagine auth stanno a ~0.65, auth↔billing a ~0.2: soglia 0.5 per i duplicati, -1 per le ricerche.
    auth_text = "Servizio di autenticazione: login utente, verifica password, emissione session token."
    calls = [
        ("wiki.upsert_concept", {"slug": "auth-service", "title": "Auth service", "sections": {"Definizione": auth_text}}),
        ("wiki.upsert_concept", {"slug": "authentication", "title": "Authentication",
                                 "sections": {"Definizione": auth_text + " Login e session token per ogni utente."}}),
        ("wiki.upsert_concept", {"slug": "billing-invoices", "title": "Billing",
                                 "sections": {"Definizione": "Fatturazione: totale fattura, righe, prezzi, IVA, pagamenti."}}),
        ("wiki.embed", {"force": True}),
        ("wiki.find_duplicates", {"threshold": 0.5, "types": ["concept"]}),
        ("wiki.search_semantic", {"query": "login utente session token password", "k": 3, "min_score": -1}),
        ("code.reindex", {"force": True, "wait": True}),
        ("code.status", {}),
        ("code.search", {"query": "authenticate user login token", "smart_level": 2, "limit": 3}),
        ("graph.semantic_neighbors", {"source": "auth-service", "k": 3, "min_score": -1}),
        ("graph.report", {"write": False}),
        ("graph.html", {}),
        ("graph.search_text", {"query": "fattura prezzi IVA pagamenti", "k": 3, "min_score": -1}),
    ]
    res = rpc(project, calls)
    by = {name: r for (name, _), r in zip(calls, res)}

    print("§1 embedding wiki")
    check("wiki.embed ok", "error" not in by["wiki.embed"], str(by["wiki.embed"])[:200])
    dup = by["wiki.find_duplicates"]
    pairs = [set(p.get("pages", [])) for p in dup.get("duplicates", [])]
    check("find_duplicates trova auth-service ↔ authentication", {"auth-service", "authentication"} in pairs, str(dup)[:300])
    check("find_duplicates NON accoppia billing con auth",
          not any("billing-invoices" in p for p in pairs), str(pairs))
    sem = by["wiki.search_semantic"]
    top = json.dumps(sem.get("results", sem))[:400]
    check("search_semantic: top risultato è una pagina auth", "auth" in top and "billing" not in top.split("auth")[0], top[:200])

    print("§2 index codice")
    check("code.reindex ok", "error" not in by["code.reindex"], str(by["code.reindex"])[:200])
    st = json.dumps(by["code.status"])
    check("code.status riporta chunk indicizzati", "error" not in by["code.status"] and ("chunk" in st or "files" in st), st[:200])
    cs = json.dumps(by["code.search"])
    check("code.search L2 (vector) trova auth.py", "error" not in by["code.search"] and "auth.py" in cs, cs[:300])

    print("§3 graph")
    check("semantic_neighbors ok", "error" not in by["graph.semantic_neighbors"], str(by["graph.semantic_neighbors"])[:200])
    check("graph.report ok", "error" not in by["graph.report"], str(by["graph.report"])[:200])
    html = by["graph.html"]
    check("graph.html scrive un file", "error" not in html and any(Path(str(v)).is_file() for v in html.values() if isinstance(v, str) and v.endswith(".html")), str(html)[:200])
    gs = json.dumps(by["graph.search_text"])
    check("graph.search_text cross-kind trova billing", "error" not in by["graph.search_text"] and "billing" in gs, gs[:300])

    print("§4 import del vecchio code-index.db senza nuovi embedding")
    probe = r"""
import sqlite3, sys, os, json, hashlib, struct
os.environ["RAIDHO_VECTOR_REDIS"] = sys.argv[3]
sys.path.insert(0, sys.argv[1])
import code_db
from pathlib import Path
root = Path(sys.argv[2]); root.mkdir()
f = root / "code-index.db"
db = sqlite3.connect(str(f))
db.executescript("CREATE TABLE chunks (id INTEGER PRIMARY KEY, file_path TEXT NOT NULL, func_name TEXT, line_start INTEGER, line_end INTEGER, content TEXT NOT NULL, lang TEXT, last_modified TEXT, content_sha TEXT, kind TEXT NOT NULL DEFAULT 'code'); CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT); CREATE TABLE indexed_files (kind TEXT, file_path TEXT, snapshot TEXT);")
db.executemany("INSERT INTO meta VALUES (?,?)", [("embed_dim","3"), ("index_fingerprint","fp"), ("index_generation","4"), ("code_index_state","ready")])
db.execute("INSERT INTO chunks(id, file_path, content, lang) VALUES (1, 'a.py', 'a', 'python')")
db.execute("INSERT INTO chunks(id, file_path, content, lang) VALUES (2, 'b.py', 'b', 'python')")
db.executemany("INSERT INTO indexed_files VALUES ('code',?,?)", [("a.py", json.dumps({"hash": "ha"})), ("b.py", json.dumps({"hash": "hb"}))])
db.commit(); db.close()
r = code_db._redis()
old = "raidhodev:vec:" + hashlib.sha1(str(f).encode()).hexdigest()[:16]
r.delete(old)
r.execute_command("VADD", old, "FP32", struct.pack("3f", 1, 0, 0), "1")
r.execute_command("VADD", old, "FP32", struct.pack("3f", 0.6, 0.8, 0), "2")
r.set(old + ":versione", "fp|4")
idx = code_db.open_db(root, dim=3)
res = code_db.vector_search(idx, [1.0, 0.0, 0.0], limit=2, kind_filter="code")
print(f.exists(), bool(r.exists(old)), [(h["id"], round(h["distance"], 2), h["rev"]) for h in res], code_db.get_meta(idx, "index_generation"))
for k in r.scan_iter(idx.base + "*"): r.delete(k)
"""
    r = subprocess.run([PY, "-c", probe, str(PLUGIN / "scripts"), str(tmp / "legacy"), TEST_REDIS], capture_output=True, text=True, timeout=60)
    out = r.stdout.strip()
    check("vecchio indice importato e file tolto", out.startswith("False False"), out or r.stderr[-300:])
    check("vettori e revisioni ripresi (distance 1 - cos)", "[(1, 0.0, 'ha'), (2, 0.4, 'hb')] 4" in out, out or r.stderr[-300:])

    pulizia = (f"import os, sys; os.environ['RAIDHO_VECTOR_REDIS'] = {TEST_REDIS!r}; sys.path.insert(0, {str(PLUGIN / 'scripts')!r}); "
               f"import code_db; r = code_db._redis(); [r.delete(k) for k in r.scan_iter(code_db.base_key({str(project / '.raidhowiki')!r}) + '*')]")
    subprocess.run([PY, "-c", pulizia], capture_output=True, timeout=30)
    shutil.rmtree(tmp, ignore_errors=True)
    print("=" * 44)
    if FAIL:
        print(f"FAIL: {FAIL} (pass {PASS})"); sys.exit(1)
    print(f"ALL PASS ({PASS})")


def test_embed_mock():
    if not redis_usable():
        try:
            import pytest
            pytest.skip("Redis con Vector Sets non raggiungibile")
        except ImportError:
            return
    main()


if __name__ == "__main__":
    main()
