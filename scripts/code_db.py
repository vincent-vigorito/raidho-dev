#!/usr/bin/env python3
"""code_db.py — wrapper sqlite-vec per `.raidhowiki/code-index.db`.

Schema:
  chunks       — metadata + content dei chunk (id, file_path, func_name, line_start, line_end, content, lang, last_modified, sha)
  chunk_vec    — virtual table vec0 con embedding per chunk (rowid = chunks.id)
  meta         — last_indexed_sha, provider_name, dim, model
  vec_dirty    — chunk cambiati da riportare su Redis (vedi sotto)

Lazy import sqlite-vec (deps esterna ~5MB). Errore graceful se manca.

Ricerca su Redis Vector Sets (Redis 8, modulo vectorset): SQLite resta la fonte
di chunk e vettori; Redis ne tiene una copia indicizzata (HNSW) su cui la ricerca
costa millisecondi invece di scorrere tutti i vettori. Se Redis manca, non
risponde o non e' allineato, si cerca su sqlite-vec come prima e lo si riallinea
in background. RAIDHO_VECTOR_REDIS sceglie l'istanza ("off" per non usarlo).
"""

import hashlib
import json
import math
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

CODE_DB_FILENAME = "code-index.db"


def _ensure_sqlite_vec(db: sqlite3.Connection) -> None:
    """Carica sqlite-vec extension. Lazy import + load."""
    try:
        import sqlite_vec  # noqa
    except ImportError:
        raise RuntimeError(
            "sqlite-vec required for code search. Install: pip install sqlite-vec"
        ) from None
    db.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(db)
    db.enable_load_extension(False)


def open_db(raidhowiki_root: Path, dim: int = 1536, create_if_missing: bool = True, provider=None, allow_dimension_mismatch: bool = False) -> sqlite3.Connection:
    """Apre/crea la code-index.db sotto `.raidhowiki/`.

    Args:
      raidhowiki_root: path a `.raidhowiki/` (NON `.raidhowiki/wiki/`)
      dim: dimensione embedding (varia per provider/model)
      create_if_missing: True → crea schema se db non esiste
    """
    db_path = raidhowiki_root / CODE_DB_FILENAME
    if raidhowiki_root.is_symlink() or db_path.is_symlink():
        raise ValueError("index state must not be a symlink")
    if not create_if_missing and not db_path.exists():
        raise FileNotFoundError(f"code-index.db not found at {db_path}")

    raidhowiki_root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    try:
        _ensure_sqlite_vec(db)
        has_meta = db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
        stored = get_meta(db, "embed_dim") if has_meta else None
        if stored and allow_dimension_mismatch:
            dim = int(stored)
        if create_if_missing:
            _init_schema(db, dim=dim, preserve_vectors=allow_dimension_mismatch)
        elif stored and int(stored) != dim:
            raise RuntimeError("embedding dimension mismatch; run a full reindex")
        if provider is not None:
            db.execute("BEGIN")
            require_fingerprint(db, provider)
        return db
    except Exception:
        db.close()
        raise


def _init_schema(db: sqlite3.Connection, dim: int, preserve_vectors: bool = False) -> None:
    """Crea tabelle se mancano. Idempotente."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            func_name TEXT,
            line_start INTEGER,
            line_end INTEGER,
            content TEXT NOT NULL,
            lang TEXT,
            last_modified TEXT,
            content_sha TEXT,
            UNIQUE(file_path, line_start, line_end)
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(file_path);
        CREATE INDEX IF NOT EXISTS idx_chunks_lang ON chunks(lang);

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS indexed_files (
            kind TEXT NOT NULL, file_path TEXT NOT NULL, snapshot TEXT NOT NULL,
            PRIMARY KEY(kind, file_path)
        );
        CREATE TABLE IF NOT EXISTS index_runs (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
            started TEXT NOT NULL, finished TEXT, pid INTEGER, error TEXT, details TEXT
        );
        CREATE TABLE IF NOT EXISTS vec_dirty (chunk_id INTEGER PRIMARY KEY);
    """)
    # vec virtual table (dim fissa una volta creata; se cambi provider serve drop+recreate)
    existing_dim = get_meta(db, "embed_dim")
    if existing_dim and int(existing_dim) != dim:
        raise RuntimeError(
            f"DB dim mismatch: existing={existing_dim} requested={dim}. "
            f"Run reindex --force (drops + rebuilds) per cambiare provider/model."
        )
    if not preserve_vectors or not db.execute("SELECT 1 FROM sqlite_master WHERE name='chunk_vec'").fetchone():
        _ensure_vec_table(db, dim)
    set_meta(db, "embed_dim", str(dim))
    _migrate_kind_column(db)
    db.commit()


VEC_METRIC = "cosine"


def _ensure_vec_table(db: sqlite3.Connection, dim: int) -> None:
    """chunk_vec con metrica coseno: `distance` = 1 - cos, quindi lo `score = 1 - distance`
    dei tool è una similarità coseno vera (prima era 1 - L2: ranking giusto, valori
    fuorvianti rispetto alle soglie 0.5/0.85 documentate come "cosine").

    DB creati con la metrica L2 (nessuna meta `embed_metric`) vengono migrati in place:
    i vettori sono gli stessi, si ricrea solo la tabella virtuale. Nessun re-embedding."""
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunk_vec'").fetchone()
    metric = get_meta(db, "embed_metric")
    if exists and metric != VEC_METRIC:
        rows = db.execute("SELECT rowid, embedding FROM chunk_vec").fetchall()
        db.execute("DROP TABLE chunk_vec")
        db.execute(f"CREATE VIRTUAL TABLE chunk_vec USING vec0(embedding float[{dim}] distance_metric={VEC_METRIC})")
        db.executemany("INSERT INTO chunk_vec(rowid, embedding) VALUES (?, ?)", [(r[0], r[1]) for r in rows])
        set_meta(db, "embed_metric", VEC_METRIC)
        set_meta(db, "embed_metric_migrated_rows", str(len(rows)))
        return
    if not exists:
        db.execute(f"CREATE VIRTUAL TABLE chunk_vec USING vec0(embedding float[{dim}] distance_metric={VEC_METRIC})")
        set_meta(db, "embed_metric", VEC_METRIC)


def _migrate_kind_column(db: sqlite3.Connection) -> None:
    """Migration idempotente: aggiunge `kind` discriminator a `chunks`.

    'code' (default, backwards-compat) | 'wiki' (entity/concept/source/analysis/session).
    Code-index esistenti vengono marchiati 'code' automaticamente via DEFAULT.
    """
    cols = {row["name"] for row in db.execute("PRAGMA table_info(chunks)").fetchall()}
    if "kind" in cols:
        return
    db.executescript("""
        ALTER TABLE chunks ADD COLUMN kind TEXT NOT NULL DEFAULT 'code';
        CREATE INDEX IF NOT EXISTS idx_chunks_kind ON chunks(kind);
        CREATE INDEX IF NOT EXISTS idx_chunks_kind_path ON chunks(kind, file_path);
    """)


def get_meta(db: sqlite3.Connection, key: str) -> Optional[str]:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(db: sqlite3.Connection, key: str, value: str, commit: bool = True) -> None:
    db.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    if commit:
        db.commit()


PIPELINE_VERSION = "3"


def fingerprint(provider) -> str:
    return json.dumps({"provider": provider.name, "model": provider.model, "dimension": provider.dim,
                       "metric": VEC_METRIC, "pipeline": PIPELINE_VERSION}, sort_keys=True)


def require_fingerprint(db, provider):
    if get_meta(db, "index_fingerprint") != fingerprint(provider):
        raise RuntimeError("embedding fingerprint missing or incompatible; run code.reindex or wiki.embed")


def validate_vectors(vectors, count, dim):
    if len(vectors) != count:
        raise ValueError(f"expected {count} vectors, received {len(vectors)}")
    for vector in vectors:
        if len(vector) != dim or any(isinstance(x, bool) or not isinstance(x, (int, float))
                                     or not math.isfinite(x) or abs(x) > 3.402823466e38 for x in vector):
            raise ValueError("invalid embedding dimensions or non-finite/float32-overflow values")
        if not any(vector):
            raise ValueError("zero vector is invalid for cosine distance")


def reset_vectors(db, dim):
    db.execute("DROP TABLE chunk_vec")
    db.execute(f"CREATE VIRTUAL TABLE chunk_vec USING vec0(embedding float[{dim}] distance_metric={VEC_METRIC})")
    db.execute("DELETE FROM chunks")
    db.execute("DELETE FROM indexed_files")
    db.execute("DELETE FROM vec_dirty")
    set_meta(db, "embed_dim", str(dim), commit=False)
    set_meta(db, "embed_metric", VEC_METRIC, commit=False)
    set_meta(db, "redis_rebuild", "1", commit=False)


def _serialize_vec(vec: list[float]) -> bytes:
    """sqlite-vec espone serialize_float32 helper."""
    import sqlite_vec
    return sqlite_vec.serialize_float32(vec)


def upsert_chunk(
    db: sqlite3.Connection,
    file_path: str,
    func_name: Optional[str],
    line_start: int,
    line_end: int,
    content: str,
    lang: str,
    last_modified: str,
    content_sha: str,
    embedding: list[float],
    kind: str = "code",
) -> int:
    """Insert o update chunk + vec. Restituisce chunk_id.

    `kind`: 'code' (default, backwards-compat) | 'wiki'. Vedi anche `upsert_wiki_page`.
    """
    # Cerca esistente
    row = db.execute(
        "SELECT id, content_sha FROM chunks WHERE file_path = ? AND line_start = ? AND line_end = ?",
        (file_path, line_start, line_end),
    ).fetchone()

    if row is not None:
        chunk_id, existing_sha = row[0], row[1]
        if existing_sha == content_sha:
            return chunk_id  # No change
        # Update content + re-embed
        db.execute(
            "UPDATE chunks SET func_name=?, content=?, lang=?, last_modified=?, content_sha=?, kind=? WHERE id=?",
            (func_name, content, lang, last_modified, content_sha, kind, chunk_id),
        )
        db.execute("DELETE FROM chunk_vec WHERE rowid = ?", (chunk_id,))
    else:
        cur = db.execute(
            "INSERT INTO chunks (file_path, func_name, line_start, line_end, content, lang, last_modified, content_sha, kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (file_path, func_name, line_start, line_end, content, lang, last_modified, content_sha, kind),
        )
        chunk_id = cur.lastrowid

    db.execute(
        "INSERT INTO chunk_vec (rowid, embedding) VALUES (?, ?)",
        (chunk_id, _serialize_vec(embedding)),
    )
    db.execute("INSERT OR IGNORE INTO vec_dirty VALUES (?)", (chunk_id,))
    return chunk_id


def upsert_wiki_page(
    db: sqlite3.Connection,
    slug: str,
    file_path: str,
    content: str,
    content_sha: str,
    last_modified: str,
    embedding: list[float],
    page_type: Optional[str] = None,
) -> int:
    """Wrapper di upsert_chunk per pagine wiki (one-shot, no line range).

    `func_name` archivia lo slug, `lang` archivia il page_type (entity/concept/...).
    `line_start=line_end=0` (necessario per UNIQUE constraint).
    """
    return upsert_chunk(
        db=db,
        file_path=file_path,
        func_name=slug,
        line_start=0,
        line_end=0,
        content=content,
        lang=page_type or "markdown",
        last_modified=last_modified,
        content_sha=content_sha,
        embedding=embedding,
        kind="wiki",
    )


def delete_chunks_for_file(
    db: sqlite3.Connection,
    file_path: str,
    kind: Optional[str] = None,
) -> int:
    """Rimuovi tutti i chunks per un file. Se `kind` passato, filtra.
    Restituisce conta righe rimosse."""
    if kind:
        rows = db.execute(
            "SELECT id FROM chunks WHERE file_path = ? AND kind = ?",
            (file_path, kind),
        ).fetchall()
    else:
        rows = db.execute("SELECT id FROM chunks WHERE file_path = ?", (file_path,)).fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        return 0
    db.execute(f"DELETE FROM chunk_vec WHERE rowid IN ({','.join('?' * len(ids))})", ids)
    db.execute(f"DELETE FROM chunks WHERE id IN ({','.join('?' * len(ids))})", ids)
    db.executemany("INSERT OR IGNORE INTO vec_dirty VALUES (?)", [(i,) for i in ids])
    return len(ids)


def list_wiki_pages(db: sqlite3.Connection) -> list[dict]:
    """Elenca tutte le pagine wiki indexate (slug + path + hash) per consistency check."""
    rows = db.execute(
        "SELECT id, func_name AS slug, file_path, content_sha, lang AS page_type, last_modified "
        "FROM chunks WHERE kind = 'wiki' ORDER BY func_name"
    ).fetchall()
    return [dict(r) for r in rows]


def vector_search(
    db: sqlite3.Connection,
    query_vec: list[float],
    limit: int = 10,
    lang_filter: Optional[str] = None,
    kind_filter: Optional[str] = None,
    exclude_id: Optional[int] = None,
) -> list[dict]:
    """Top-k vector search per cosine distance. Joina con chunks per metadata.

    sqlite-vec knn richiede `k = ?` predicate (più efficiente di LIMIT plain).
    Filtri lang/kind/exclude_id applicati in Python su over-fetch (3×).
    """
    needs_post_filter = bool(lang_filter or kind_filter or exclude_id is not None)
    k = limit * 3 if needs_post_filter else limit
    rows = _cerca_redis(db, query_vec, k)
    if rows is not None:
        return _filtra(rows, limit, lang_filter, kind_filter, exclude_id) if needs_post_filter else rows[:limit]
    sql = """
        SELECT c.id, c.file_path, c.func_name, c.line_start, c.line_end,
               c.content, c.lang, c.kind, c.last_modified, v.distance
        FROM chunk_vec v
        JOIN chunks c ON c.id = v.rowid
        WHERE v.embedding MATCH ? AND k = ?
        ORDER BY v.distance
    """
    params: list = [_serialize_vec(query_vec), k]
    rows = db.execute(sql, params).fetchall()
    if needs_post_filter:
        out = []
        for r in rows:
            if lang_filter and r["lang"] != lang_filter:
                continue
            if kind_filter and r["kind"] != kind_filter:
                continue
            if exclude_id is not None and r["id"] == exclude_id:
                continue
            out.append(r)
            if len(out) >= limit:
                break
        rows = out
    else:
        rows = rows[:limit]
    return [dict(row) for row in rows]


def _filtra(rows, limit, lang_filter, kind_filter, exclude_id):
    out = []
    for r in rows:
        if (lang_filter and r["lang"] != lang_filter) or (kind_filter and r["kind"] != kind_filter) \
                or (exclude_id is not None and r["id"] == exclude_id):
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


# --- Redis Vector Sets -------------------------------------------------------

REDIS_DEFAULT = "redis://127.0.0.1:6379/14"
_SYNC_LOCK = threading.Lock()


def _redis():
    url = os.environ.get("RAIDHO_VECTOR_REDIS", REDIS_DEFAULT)
    if url.lower() in ("", "off", "none"):
        return None
    try:
        import redis
        r = redis.Redis.from_url(url, socket_timeout=10, socket_connect_timeout=2)
        r.ping()
        return r
    except Exception:
        return None


def _db_path(db) -> str:
    return next(row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main")


def _chiave(db) -> str:
    """Un vector set per indice: il path del DB lo rende unico anche con piu' progetti per container."""
    return "raidhodev:vec:" + hashlib.sha1(_db_path(db).encode()).hexdigest()[:16]


def _versione(db) -> str:
    return f"{get_meta(db, 'index_fingerprint') or ''}|{get_meta(db, 'index_generation') or '0'}"


def _cerca_redis(db, query_vec, k):
    """Righe come vector_search, o None se si deve cercare su SQLite (Redis assente o
    non allineato: in quel caso parte il riallineamento in background)."""
    r = _redis()
    if r is None:
        return None
    try:
        key = _chiave(db)
        sporchi = db.execute("SELECT 1 FROM sqlite_master WHERE name='vec_dirty'").fetchone() and \
            db.execute("SELECT 1 FROM vec_dirty LIMIT 1").fetchone()
        if sporchi or get_meta(db, "redis_rebuild") or (r.get(key + ":versione") or b"").decode() != _versione(db):
            _sync_in_background(_db_path(db))
            return None
        raw = r.execute_command("VSIM", key, "FP32", _serialize_vec(query_vec), "WITHSCORES", "COUNT", k)
    except Exception:
        return None
    coppie = raw.items() if isinstance(raw, dict) else zip(raw[::2], raw[1::2])
    # VSIM da' (1 + coseno) / 2: la distanza coseno usata qui sotto e' 1 - coseno
    distanze = {int(el): 2.0 - 2.0 * float(score) for el, score in coppie}
    if not distanze:
        return []
    righe = db.execute(
        "SELECT id, file_path, func_name, line_start, line_end, content, lang, kind, last_modified FROM chunks "
        f"WHERE id IN ({','.join('?' * len(distanze))})", list(distanze)).fetchall()
    out = [{**dict(row), "distance": distanze[row["id"]]} for row in righe]
    return sorted(out, key=lambda x: x["distance"])


def _sync_in_background(path: str) -> None:
    if _SYNC_LOCK.locked():
        return

    def giro():
        with _SYNC_LOCK:
            try:
                conn = open_db(Path(path).parent, create_if_missing=False, allow_dimension_mismatch=True)
                try:
                    redis_sync(conn)
                finally:
                    conn.close()
            except Exception:
                pass
    threading.Thread(target=giro, name="raidhodev-redis-sync", daemon=True).start()


def redis_sync(db, lotto: int = 500) -> dict:
    """Porta su Redis i chunk cambiati (vec_dirty) o, se Redis non e' allineato, lo
    ricostruisce da SQLite su una chiave nuova scambiata alla fine (chi cerca non vede
    mai un indice a meta'). Niente embedding: i vettori sono gia' in SQLite."""
    r = _redis()
    if r is None:
        return {"redis": False}
    db.execute("CREATE TABLE IF NOT EXISTS vec_dirty (chunk_id INTEGER PRIMARY KEY)")
    db.commit()
    key = _chiave(db)
    if not r.set(key + ":lock", str(os.getpid()), nx=True, ex=900):
        return {"redis": True, "occupato": True}
    try:
        versione = _versione(db)
        ricostruire = bool(get_meta(db, "redis_rebuild")) or not r.exists(key) or \
            r.get(key + ":versione") is None or \
            int(r.execute_command("VCARD", key) or 0) + db.execute("SELECT COUNT(*) FROM vec_dirty").fetchone()[0] \
            < db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        sporchi = [row[0] for row in db.execute("SELECT chunk_id FROM vec_dirty")]
        if ricostruire:
            nuova = key + ":nuova"
            r.delete(nuova)
            cur = db.execute("SELECT rowid, embedding FROM chunk_vec")
            n = 0
            while blocco := cur.fetchmany(lotto):
                pipe = r.pipeline(transaction=False)
                for rid, blob in blocco:
                    pipe.execute_command("VADD", nuova, "FP32", blob, str(rid))
                pipe.execute()
                n += len(blocco)
            if n:
                r.rename(nuova, key)
            else:
                r.delete(key)
            esito = {"redis": True, "ricostruito": n}
        else:
            for i in range(0, len(sporchi), lotto):
                parte = sporchi[i:i + lotto]
                vettori = dict(db.execute(
                    f"SELECT rowid, embedding FROM chunk_vec WHERE rowid IN ({','.join('?' * len(parte))})", parte).fetchall())
                pipe = r.pipeline(transaction=False)
                for cid in parte:
                    if cid in vettori:
                        pipe.execute_command("VADD", key, "FP32", vettori[cid], str(cid))
                    else:
                        pipe.execute_command("VREM", key, str(cid))
                pipe.execute()
            esito = {"redis": True, "aggiornati": len(sporchi)}
        if sporchi:
            db.executemany("DELETE FROM vec_dirty WHERE chunk_id = ?", [(c,) for c in sporchi])
        db.execute("DELETE FROM meta WHERE key = 'redis_rebuild'")
        db.commit()
        r.set(key + ":versione", versione)
        return esito
    finally:
        r.delete(key + ":lock")


def compatta_se_serve(db, soglia: float = 0.3) -> bool:
    """VACUUM quando oltre `soglia` delle pagine sono libere (dopo molti reindex il file
    resta gonfio e la ricerca su SQLite legge anche quelle)."""
    pagine = db.execute("PRAGMA page_count").fetchone()[0]
    libere = db.execute("PRAGMA freelist_count").fetchone()[0]
    if pagine and libere / pagine > soglia:
        db.commit()
        db.execute("VACUUM")
        return True
    return False


def get_embedding_by_source(
    db: sqlite3.Connection,
    source: str,
    kind: Optional[str] = None,
) -> Optional[dict]:
    """Trova il primo chunk per source (file_path o slug-as-file_path) + kind.

    Ritorna dict con id + metadata (no embedding raw, serve solo l'id per query knn).
    """
    if kind:
        row = db.execute(
            "SELECT id, file_path, func_name, line_start, line_end, lang, kind FROM chunks "
            "WHERE (file_path = ? OR func_name = ?) AND kind = ? LIMIT 1",
            (source, source, kind),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT id, file_path, func_name, line_start, line_end, lang, kind FROM chunks "
            "WHERE file_path = ? OR func_name = ? LIMIT 1",
            (source, source),
        ).fetchone()
    return dict(row) if row else None


def get_embedding_vector(db: sqlite3.Connection, chunk_id: int) -> Optional[list[float]]:
    """Ritorna l'embedding raw per un chunk_id (per usarlo come query knn altrove)."""
    row = db.execute(
        "SELECT embedding FROM chunk_vec WHERE rowid = ?", (chunk_id,)
    ).fetchone()
    if not row:
        return None
    import struct
    blob = row[0]
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def stats(db: sqlite3.Connection) -> dict:
    """Statistiche dell'index."""
    total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    by_lang = {
        row[0]: row[1]
        for row in db.execute("SELECT lang, COUNT(*) FROM chunks GROUP BY lang ORDER BY 2 DESC").fetchall()
    }
    by_kind = {
        row[0]: row[1]
        for row in db.execute("SELECT kind, COUNT(*) FROM chunks GROUP BY kind ORDER BY 2 DESC").fetchall()
    }
    last_modified = db.execute("SELECT MAX(last_modified) FROM chunks").fetchone()[0]
    return {
        "total_chunks": total,
        "by_lang": by_lang,
        "by_kind": by_kind,
        "last_modified": last_modified,
        "embed_dim": get_meta(db, "embed_dim"),
        "embed_provider": get_meta(db, "embed_provider"),
        "embed_model": get_meta(db, "embed_model"),
        "last_indexed_sha": get_meta(db, "last_indexed_sha"),
    }
