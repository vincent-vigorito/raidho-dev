"""Snapshot filesystem → embedding → pubblicazione atomica su Redis (vedi code_db)."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import index_policy

import code_db
import code_index
import embed_providers
import skill_parser

MAX_FILE_BYTES = 500_000


def now():
    return datetime.now(timezone.utc).isoformat()


def snapshot_file(path):
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        data = stream.read(MAX_FILE_BYTES + 1)
    reason = "too_large" if len(data) > MAX_FILE_BYTES else ("empty" if not data.strip() else None)
    digest = (f"oversize:{stat.st_size}:{stat.st_mtime_ns}" if reason == "too_large"
              else hashlib.sha256(data).hexdigest())
    return {"hash": digest, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "excluded": reason}, (
        "" if reason else data.decode("utf-8", errors="replace"))


def discover(root, kind, include_sessions=False, single=None):
    return index_policy.discover(root, kind, include_sessions, single)[0]


def file_key(root, kind, path):
    return str(path.relative_to(root)) if kind == "code" else str(path)


def snapshot(root, kind, include_sessions=False, single=None):
    return {file_key(root, kind, path): snapshot_file(path)[0]
            for path in discover(root, kind, include_sessions, single)}


def content_equal(first, second):
    return first is not None and first["hash"] == second["hash"] and first["excluded"] == second["excluded"]


def chunks(root, kind, path, text):
    if kind == "code":
        return [{**chunk, "lang": code_index.LANG_BY_EXT[path.suffix.lower()]}
                for chunk in code_index.chunk_text(text, path.suffix)]
    import wiki_embed
    meta, body = skill_parser.parse_frontmatter(text)
    meta = meta if isinstance(meta, dict) else {}
    content = wiki_embed._compute_input_text(meta, body)
    return [{"content": content, "func_name": wiki_embed._slug_from_path(path, root / ".raidhowiki" / "wiki"),
             "line_start": 0, "line_end": 0, "lang": str(meta.get("type") or "page")}]


def refresh(root, kind, **kwargs):
    if kwargs.pop("dry_run", False):
        try:
            return index_policy.preview(root, kind, kwargs.get("include_sessions", False), kwargs.get("single"))
        except Exception as exc:
            return {"error": str(exc), "code": "index_policy_error", "dry_run": True}
    started = time.monotonic()
    for attempt in range(3):
        result = _refresh(root, kind, **kwargs)
        result["attempts"] = attempt + 1
        if result.get("error") != "another index was published during embedding; retry":
            break
    result["ms"] = round((time.monotonic() - started) * 1000)
    return result


# oltre questi chunk da rifare l'indice si ricostruisce su chiavi nuove invece che in un'unica transazione
REBUILD_CHUNKS = 3000

# tetto ai caratteri di una richiesta di embedding, oltre ai batch_size chunk
MAX_BATCH_CHARS = 200_000


def _batches(staged, batch_size):
    """Intervalli [start, end) di staged: al massimo batch_size chunk e MAX_BATCH_CHARS caratteri (almeno un chunk)."""
    start = 0
    while start < len(staged):
        end, size = start, 0
        while end < len(staged) and end - start < batch_size:
            size += len(staged[end][2]["content"])
            if end > start and size > MAX_BATCH_CHARS:
                break
            end += 1
        yield start, end
        start = end


def _chunk_json(scope, path, chunk, info):
    return json.dumps({"file_path": path, "func_name": chunk["func_name"], "line_start": chunk["line_start"],
                       "line_end": chunk["line_end"], "col_start": chunk.get("col_start"), "col_end": chunk.get("col_end"),
                       "content": chunk["content"], "lang": chunk["lang"], "kind": scope,
                       "last_modified": datetime.fromtimestamp(info["mtime_ns"] / 1e9, timezone.utc).isoformat(),
                       "content_sha": hashlib.sha256(chunk["content"].encode()).hexdigest(), "rev": info["hash"]})


def _refresh(root, kind, force=False, limit=None, include_sessions=False, single=None, batch_size=64, verbose=False, expected_snapshot=None, expected_fingerprint=None):
    root = Path(root).resolve()
    directory = root / ".raidhowiki"
    if directory.is_symlink() or not directory.resolve().is_relative_to(root):
        return {"error": "project state must remain inside root", "status": "failed", "code": "unsafe_project_state"}
    if not directory.is_dir():
        return {"error": f"`.raidhowiki/` not found under {root}", "status": "failed", "code": "project_missing"}
    if limit is not None and (not isinstance(limit, int) or limit <= 0):
        return {"error": "limit must be a positive integer", "status": "failed", "code": "invalid_index_options"}
    if not isinstance(batch_size, int) or batch_size <= 0:
        return {"error": "batch_size must be positive", "status": "failed", "code": "invalid_index_options"}
    idx = None
    run = None
    lock = None
    run_id = uuid.uuid4().hex
    state = "failed"
    details = {}
    try:
        index_policy.authorize(root)
        provider = embed_providers.get_provider()
        if provider is None:
            return {"error": "no embed provider available (set RAIDHO_EMBED_PROVIDER + API key)", "status": "failed", "code": "provider_unavailable"}
        index_policy.authorize(root, provider)
        if not isinstance(provider.dim, int) or provider.dim <= 0:
            raise ValueError("invalid provider dimension")
        idx = code_db.open_db(directory, dim=provider.dim, allow_dimension_mismatch=True)
        meta = idx.meta()
        generation = meta.get("index_generation") or "0"
        old_fp = meta.get("index_fingerprint")
        new_fp = code_db.fingerprint(provider)
        if expected_fingerprint is not None and expected_fingerprint != new_fp:
            return {"status": "superseded", "error": "job fingerprint changed"}
        if expected_snapshot is not None:
            discover(root, "wiki", single=single)
            actual = snapshot_file(Path(single))[0]["hash"] if Path(single).is_file() else "missing"
            if actual != expected_snapshot:
                return {"status": "superseded", "error": "job content changed"}

        manifest = code_db.manifest(idx)
        esistenti = code_db.file_chunk_ids(idx)
        # un vecchio code-index.db non importabile: si ricostruiscono entrambi gli scope, come in una migrazione
        legacy = (directory / code_db.CODE_DB_FILENAME).is_file() and not idx.built()
        migration = (old_fp != new_fp and bool(old_fp or esistenti or manifest or legacy)) or int(meta.get("embed_dim") or provider.dim) != provider.dim
        run = {"id": run_id, "kind": kind, "status": "building", "started": now(), "pid": os.getpid(), "details": {}}
        code_db.set_run(idx, kind, run)
        if migration and limit is not None:
            state = "partial"
            raise ValueError("model/pipeline migration requires a complete rebuild without limit")
        if migration:
            from project_recovery import snapshot_project
            backup = snapshot_project(root, reason="embedding_migration")
            details["backup"] = backup
        scopes = ("code", "wiki") if migration else (kind,)
        old_sessions = any(k == "wiki" and Path(p).is_relative_to(directory / "wiki" / "sessions") for k, p in esistenti)
        sessions = include_sessions or (migration and old_sessions)
        # Un job single non può pubblicare un modello nuovo per una sola pagina.
        selected_single = None if migration else single
        current = {}
        selected = set()
        deleted = set()
        for scope in scopes:
            scan = snapshot(root, scope, sessions, selected_single if scope == "wiki" else None)
            current.update({(scope, path): info for path, info in scan.items()})
            changed = [(scope, path) for path, info in scan.items()
                       if force or migration or not content_equal(manifest.get((scope, path)), info)]
            selected.update(changed[:limit] if limit is not None else changed)
            old_paths = {path for k, path in manifest if k == scope}
            old_paths.update(path for k, path in esistenti if k == scope)
            if scope == "wiki":
                if selected_single is not None:
                    old_paths &= {str(selected_single)}
                elif not sessions:
                    old_paths = {p for p in old_paths if not Path(p).is_relative_to(directory / "wiki" / "sessions")}
            deleted.update((scope, p) for p in old_paths - scan.keys())
        deferred = [path for (scope, path), info in current.items()
                    if (scope, path) not in selected and not content_equal(manifest.get((scope, path)), info)]
        details = {**details, "scopes": list(scopes), "migration": migration, "deferred_files": deferred,
                   "excluded_files": [{"path": p, "reason": info["excluded"]}
                                      for (_k, p), info in current.items() if info["excluded"]],
                   "selected_files": len(selected), "deleted_files": len(deleted)}
        run["details"] = details
        code_db.set_run(idx, kind, run)
        sha = code_index.get_current_git_sha(root)

        # file cambiati mentre si indicizza (agenti al lavoro): non fermano il giro,
        # restano indietro col contenuto vecchio e li riprende il giro successivo
        moved = set()
        staged = []
        for scope, path in sorted(selected):
            source = root / path if scope == "code" else Path(path)
            info, text = snapshot_file(source)
            if info != current[(scope, path)]:
                moved.add((scope, path))
                continue
            if info["excluded"]:
                continue
            for chunk in chunks(root, scope, source, text):
                staged.append((scope, path, chunk))
        r = idx.r
        rebuild = migration or force or not idx.built() or len(staged) > REBUILD_CHUNKS
        nuovo = idx.k("new")
        if rebuild:
            # le chiavi :new:* sono una per progetto: una ricostruzione alla volta
            lock = code_db.prendi_lock(idx, run_id)
            if lock is None:
                state = "stale"
                raise RuntimeError("another index rebuild is running; retry later")
            r.delete(*(f"{nuovo}:{n}" for n in ("vec", "chunks", "files", "filechunks")))
        ids = []
        if staged:
            fine = r.incrby(idx.k("nextid"), len(staged))
            ids = list(range(fine - len(staged) + 1, fine + 1))
        vettori = {}
        indexed_chunks = 0
        for start, end in _batches(staged, batch_size):
            batch = staged[start:end]
            for scope in scopes:
                admitted = {file_key(root, scope, path) for path in discover(root, scope, sessions, selected_single if scope == "wiki" else None)}
                # file nuovi o cancellati sono lavoro normale; si ferma solo se la
                # policy esclude file che ci sono ancora
                esclusi = {path for k, path in current if k == scope} - admitted
                if any((root / p if scope == "code" else Path(p)).is_file() for p in esclusi):
                    state = "stale"
                    raise RuntimeError("discovery policy changed before embedding batch")
            index_policy.authorize(root, provider)
            vectors = provider.embed([chunk["content"] for _s, _p, chunk in batch])
            code_db.validate_vectors(vectors, len(batch), provider.dim)
            if rebuild:
                # le chiavi nuove non le vede nessuno fino allo scambio: niente da tenere in memoria
                pipe = r.pipeline(transaction=False)
                for (scope, path, chunk), cid, vec in zip(batch, ids[start:], vectors):
                    pipe.execute_command("VADD", f"{nuovo}:vec", "FP32", code_db.fp32(vec), str(cid),
                                         "SETATTR", code_db.attributi({**chunk, "kind": scope}))
                    pipe.hset(f"{nuovo}:chunks", str(cid), _chunk_json(scope, path, chunk, current[(scope, path)]))
                pipe.execute()
            else:
                vettori.update({cid: code_db.fp32(vec) for cid, vec in zip(ids[start:], vectors)})
            indexed_chunks += len(batch)

        latest = {}
        for scope in scopes:
            latest.update({(scope, p): info for p, info in snapshot(root, scope, sessions, selected_single if scope == "wiki" else None).items()})
        moved |= {key for key, info in current.items() if latest.get(key) != info}
        nuovi = latest.keys() - current.keys()
        pubblicati = selected - moved
        via = pubblicati | deleted
        per_file = {}
        for (scope, path, chunk), cid in zip(staged, ids):
            if (scope, path) in pubblicati:
                per_file.setdefault((scope, path), {"ids": [], "lang": chunk["lang"]})["ids"].append(cid)
        new_manifest = {} if migration else dict(manifest)
        for key in via:
            new_manifest.pop(key, None)
        for key, info in current.items():
            if key not in moved and (key in selected or content_equal(manifest.get(key), info)):
                new_manifest[key] = info
        state = "stale" if moved or nuovi else "partial" if deferred or selected_single is not None else "ready"
        finished = now()
        details["moved_files"] = sorted(p for _k, p in moved | nuovi)

        if rebuild:
            pipe = r.pipeline(transaction=False)
            # i file mossi durante l'embedding escono dalle chiavi nuove...
            for (scope, path, _chunk), cid in zip(staged, ids):
                if (scope, path) in moved:
                    pipe.execute_command("VREM", f"{nuovo}:vec", str(cid))
                    pipe.hdel(f"{nuovo}:chunks", str(cid))
            pipe.execute()
            # ...e, fuori da una migrazione, restano col contenuto vecchio come i file non toccati
            tenuti = {} if migration else {key: lst for key, lst in esistenti.items() if key not in via}
            tenuti_ids = [i for lst in tenuti.values() for i in lst]
            copiati = {}
            for i in range(0, len(tenuti_ids), 500):
                parte = tenuti_ids[i:i + 500]
                vecchi = r.hmget(idx.k("chunks"), [str(c) for c in parte])
                pipe = r.pipeline(transaction=False)
                for cid in parte:
                    pipe.execute_command("VEMB", idx.k("vec"), str(cid))
                emb = pipe.execute(raise_on_error=False)
                pipe = r.pipeline(transaction=False)
                for cid, raw, v in zip(parte, vecchi, emb):
                    if raw is None or not v or isinstance(v, Exception):
                        continue
                    vecchio = json.loads(raw)
                    copiati[cid] = vecchio.get("lang") or ""
                    pipe.execute_command("VADD", f"{nuovo}:vec", "FP32", code_db.fp32([float(x) for x in v]), str(cid),
                                         "SETATTR", code_db.attributi(vecchio))
                    pipe.hset(f"{nuovo}:chunks", str(cid), raw)
                pipe.execute()
            tenuti_file = {}
            for key, lst in tenuti.items():
                ok = [c for c in lst if c in copiati]
                if ok:
                    tenuti_file[key] = {"ids": ok, "lang": copiati[ok[0]]}
            pipe = r.pipeline(transaction=False)
            for key, dati in {**tenuti_file, **per_file}.items():
                pipe.hset(f"{nuovo}:filechunks", code_db.fkey(*key), json.dumps(dati))
            for key, info in new_manifest.items():
                pipe.hset(f"{nuovo}:files", code_db.fkey(*key), json.dumps(info))
            pipe.execute()

        def pubblica(pipe):
            current_generation = code_db._s(pipe.hget(idx.k("meta"), "index_generation")) or "0"
            if current_generation != generation:
                concurrent_fp = code_db._s(pipe.hget(idx.k("meta"), "index_fingerprint"))
                if rebuild or migration or concurrent_fp != new_fp or latest != current:
                    raise code_db.ConcurrentPublish("another index was published during embedding; retry")
            vecchi = {}
            if not rebuild and via:
                campi = [code_db.fkey(*key) for key in via]
                for key, raw in zip(via, pipe.hmget(idx.k("filechunks"), campi)):
                    if raw is not None:
                        vecchi[key] = json.loads(raw)["ids"]
            pipe.multi()
            if rebuild:
                for n in ("vec", "chunks", "files", "filechunks"):
                    if r.exists(f"{nuovo}:{n}"):
                        pipe.rename(f"{nuovo}:{n}", idx.k(n))
                    else:
                        pipe.delete(idx.k(n))
            else:
                for key, lst in vecchi.items():
                    for cid in lst:
                        pipe.execute_command("VREM", idx.k("vec"), str(cid))
                    if lst:
                        pipe.hdel(idx.k("chunks"), *[str(c) for c in lst])
                    pipe.hdel(idx.k("filechunks"), code_db.fkey(*key))
                for (scope, path, chunk), cid in zip(staged, ids):
                    if (scope, path) not in pubblicati:
                        continue
                    pipe.execute_command("VADD", idx.k("vec"), "FP32", vettori[cid], str(cid),
                                         "SETATTR", code_db.attributi({**chunk, "kind": scope}))
                    pipe.hset(idx.k("chunks"), str(cid), _chunk_json(scope, path, chunk, current[(scope, path)]))
                for key, dati in per_file.items():
                    pipe.hset(idx.k("filechunks"), code_db.fkey(*key), json.dumps(dati))
                tolti = [code_db.fkey(*key) for key in manifest if key not in new_manifest]
                if tolti:
                    pipe.hdel(idx.k("files"), *tolti)
                for key, info in new_manifest.items():
                    if manifest.get(key) != info:
                        pipe.hset(idx.k("files"), code_db.fkey(*key), json.dumps(info))
            metadata = {"embed_provider": provider.name, "embed_model": provider.model, "embed_dim": str(provider.dim),
                        "embed_metric": code_db.VEC_METRIC, "index_fingerprint": new_fp,
                        "index_generation": str(int(current_generation) + 1)}
            for scope in scopes:
                metadata[scope + "_index_state"] = state
                # anche stale e' una pubblicazione riuscita: mancano solo i file mossi nel frattempo
                if state in ("ready", "stale"):
                    metadata[scope + "_last_success"] = finished
            if "code" in scopes and state == "ready":
                metadata["last_indexed_sha"] = sha or ""
                metadata["last_indexed_at"] = finished
            pipe.hset(idx.k("meta"), mapping=metadata)
            code_db.set_run(idx, kind, {**run, "status": state, "finished": finished, "details": details}, pipe)

        import redis
        try:
            r.transaction(pubblica, idx.k("meta"))
        except redis.WatchError:
            raise code_db.ConcurrentPublish("another index was published during embedding; retry") from None
        return {"status": state, "indexed_files": len(pubblicati), "indexed_chunks": indexed_chunks,
                "scanned": len(current), "embedded": indexed_chunks, "deleted_orphans": len(deleted),
                "skipped_unchanged": len(current) - len(selected) - len(deferred),
                "provider": provider.name, "model": provider.model, "dim": provider.dim, "git_sha": sha,
                "incremental": not rebuild, "errors": [], **details}
    except Exception as exc:
        if isinstance(exc, code_db.ConcurrentPublish):
            state = "stale"
        if state not in ("stale", "partial"):
            state = "failed"
        if idx is not None and run is not None:
            try:
                code_db.set_run(idx, kind, {**run, "status": state, "finished": now(), "error": str(exc), "details": details})
            except Exception:
                pass
        return {"error": str(exc), "errors": [str(exc)], "status": state, "code": getattr(exc, "code", "index_build_failed"), "indexed_files": 0, "indexed_chunks": 0, **details}
    finally:
        if idx is not None:
            try:
                if lock:
                    idx.r.delete(*(f"{idx.k('new')}:{n}" for n in ("vec", "chunks", "files", "filechunks")))
                    code_db.lascia_lock(idx, lock)
            except Exception:
                pass
            idx.close()


def index_status(root, provider=None):
    root = Path(root).resolve()
    r = code_db._redis()
    if r is None:
        return {"indexed": False, "status": "unavailable", "hint": "Redis not reachable: semantic index unavailable"}
    idx = code_db.Index(r, root / ".raidhowiki")
    try:
        code_db.importa_legacy(idx)
    except Exception:
        pass
    meta = idx.meta()
    runs = {kind: code_db.get_run(idx, kind) for kind in code_db.KINDS}
    if not meta.get("index_fingerprint") and not any(runs.values()):
        return {"indexed": False, "status": "missing", "hint": "Run code.reindex"}
    manifest = code_db.manifest(idx)
    compatible = provider is not None and meta.get("index_fingerprint") == code_db.fingerprint(provider)
    scopes = {}
    for kind in code_db.KINDS:
        attempt = runs[kind]
        state = meta.get(kind + "_index_state", "missing")
        changed = []
        if attempt:
            if attempt["started"] > meta.get(kind + "_last_success", ""):
                state = attempt["status"]
            if state == "building":
                try:
                    os.kill(attempt["pid"], 0)
                except ProcessLookupError:
                    state = "failed"
                    attempt["error"] = "index process interrupted before completion"
                except PermissionError:
                    pass
        if not compatible:
            state = "incompatible" if provider is not None else "provider_unavailable"
        elif state not in ("building", "failed"):
            previous = {p: info for (k, p), info in manifest.items() if k == kind}
            sessions = any(Path(p).is_relative_to(root / ".raidhowiki/wiki/sessions") for p in previous) if kind == "wiki" else False
            try:
                current = snapshot(root, kind, sessions)
                changed = sorted(p for p in current if not content_equal(previous.get(p), current[p]))
                changed += sorted(previous.keys() - current.keys())
                if changed and state != "partial":
                    state = "stale"
            except OSError as exc:
                state = "stale"
                changed = [str(exc)]
        scopes[kind] = {"status": state, "last_success": meta.get(kind + "_last_success"),
                        "changed_files": changed, "last_attempt": attempt}
    result = code_db.stats(idx)
    result.update({"indexed": bool(meta.get("index_fingerprint")), "status": scopes["code"]["status"],
                   "scopes": scopes, "fingerprint": meta.get("index_fingerprint"), "store": "redis", "redis_key": idx.base})
    return result
