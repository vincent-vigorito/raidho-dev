#!/usr/bin/env python3
"""code_db.py — indice semantico di codice e wiki su Redis (Redis 8, Vector Sets).

Tutto sta in Redis, sotto un prefisso per progetto (`raidhodev:idx:<hash di .raidhowiki>`):
  :vec          Vector Set, un elemento per chunk (id numerico) con attributi
                {"kind", "lang"} per FILTER; quantizzazione Q8, metrica coseno
  :chunks       hash id → JSON del chunk (file_path, func_name, righe, content, lang,
                kind, last_modified, content_sha, rev = hash del file indicizzato)
  :filechunks   hash "<kind>:<path>" → JSON {"ids": [...], "lang": ...}
  :files        hash "<kind>:<path>" → JSON dello snapshot (manifest dell'incrementale)
  :meta         hash fingerprint, generazione, stato per scope, ultimi successi
  :run:<kind>   JSON dell'ultima run (stato, pid, errore, dettagli)
  :nextid       contatore degli id dei chunk

La pubblicazione e' atomica per chi cerca: l'incrementale e' un MULTI/EXEC sotto WATCH
di :meta, la ricostruzione completa scrive su :new:* e scambia le chiavi con RENAME.
RAIDHO_VECTOR_REDIS sceglie l'istanza. Se Redis manca la ricerca vettoriale non c'e'
e i tool ripiegano sulla ricerca lessicale.

Il vecchio `.raidhowiki/code-index.db` (SQLite + sqlite-vec) si importa una volta, senza
nuovi embedding, se la sua copia su Redis era allineata; poi il file si toglie.
"""

import hashlib
import json
import math
import os
import sqlite3
import struct
from pathlib import Path
from typing import Iterable, Optional

CODE_DB_FILENAME = "code-index.db"   # solo il vecchio formato, per l'import
REDIS_DEFAULT = "redis://127.0.0.1:6379/14"
PIPELINE_VERSION = "3"
VEC_METRIC = "cosine"
KINDS = ("code", "wiki")


class IndexUnavailable(RuntimeError):
    pass


class ConcurrentPublish(RuntimeError):
    pass


def _redis():
    url = os.environ.get("RAIDHO_VECTOR_REDIS", REDIS_DEFAULT)
    if url.lower() in ("", "off", "none"):
        return None
    try:
        import redis
        r = redis.Redis.from_url(url, socket_timeout=60, socket_connect_timeout=2)
        r.ping()
        return r
    except Exception:
        return None


def redis_ok() -> bool:
    r = _redis()
    if r is None:
        return False
    try:
        return any((m.get(b"name") or m.get("name")) in (b"vectorset", "vectorset") for m in r.module_list())
    except Exception:
        return False


def base_key(raidhowiki_root) -> str:
    return "raidhodev:idx:" + hashlib.sha1(str(Path(raidhowiki_root).resolve()).encode()).hexdigest()[:16]


def fkey(kind: str, path: str) -> str:
    return f"{kind}:{path}"


def split_fkey(field) -> tuple[str, str]:
    field = field.decode() if isinstance(field, bytes) else field
    kind, path = field.split(":", 1)
    return kind, path


def _s(v):
    return v.decode() if isinstance(v, bytes) else v


class Index:
    """Maniglia dell'indice di un progetto (al posto della vecchia connessione SQLite)."""

    def __init__(self, r, directory):
        self.r = r
        self.directory = Path(directory)
        self.base = base_key(directory)

    def k(self, name: str, prefix: Optional[str] = None) -> str:
        return f"{prefix or self.base}:{name}"

    def close(self):
        pass

    def meta(self) -> dict:
        return {_s(a): _s(b) for a, b in self.r.hgetall(self.k("meta")).items()}

    def built(self) -> bool:
        return bool(self.r.hexists(self.k("meta"), "index_fingerprint"))


def open_db(raidhowiki_root: Path, dim: int = 1536, create_if_missing: bool = True, provider=None,
            allow_dimension_mismatch: bool = False) -> Index:
    """L'indice del progetto. create_if_missing=False: FileNotFoundError se non e' mai stato costruito."""
    raidhowiki_root = Path(raidhowiki_root)
    if raidhowiki_root.is_symlink():
        raise ValueError("index state must not be a symlink")
    r = _redis()
    if r is None:
        raise IndexUnavailable("Redis not reachable (RAIDHO_VECTOR_REDIS): semantic index unavailable")
    idx = Index(r, raidhowiki_root)
    importa_legacy(idx)
    stored = get_meta(idx, "embed_dim")
    if not idx.built() and not create_if_missing:
        raise FileNotFoundError(f"semantic index not built for {raidhowiki_root}")
    if stored and int(stored) != dim and not allow_dimension_mismatch and not create_if_missing:
        raise RuntimeError("embedding dimension mismatch; run a full reindex")
    if provider is not None:
        require_fingerprint(idx, provider)
    return idx


def exists(raidhowiki_root) -> bool:
    """C'e' un indice pubblicato per il progetto (importando il vecchio file se serve)."""
    r = _redis()
    if r is None:
        return False
    idx = Index(r, raidhowiki_root)
    try:
        importa_legacy(idx)
    except Exception:
        pass
    return idx.built()


def get_meta(idx: Index, key: str) -> Optional[str]:
    return _s(idx.r.hget(idx.k("meta"), key))


def set_meta(idx: Index, key: str, value: str, commit: bool = True) -> None:
    idx.r.hset(idx.k("meta"), key, value)


def fingerprint(provider) -> str:
    return json.dumps({"provider": provider.name, "model": provider.model, "dimension": provider.dim,
                       "metric": VEC_METRIC, "pipeline": PIPELINE_VERSION}, sort_keys=True)


def require_fingerprint(idx, provider):
    if get_meta(idx, "index_fingerprint") != fingerprint(provider):
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


def fp32(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def attributi(chunk: dict) -> str:
    return json.dumps({"kind": chunk.get("kind") or "code", "lang": chunk.get("lang") or ""})


# --- lettura ------------------------------------------------------------------

def _filtro(lang_filter, kind_filter) -> Optional[str]:
    parti = []
    for campo, valore in (("kind", kind_filter), ("lang", lang_filter)):
        if valore:
            if '"' in valore or "\\" in valore:
                raise ValueError(f"invalid {campo} filter")
            parti.append(f'.{campo} == "{valore}"')
    return " and ".join(parti) or None


def vector_search(idx: Index, query_vec: list[float], limit: int = 10, lang_filter: Optional[str] = None,
                  kind_filter: Optional[str] = None, exclude_id: Optional[int] = None) -> list[dict]:
    """Top-k per coseno. `distance` = 1 - coseno (VSIM da' (1 + coseno) / 2)."""
    args = ["VSIM", idx.k("vec"), "FP32", fp32(query_vec), "WITHSCORES", "COUNT", limit + (1 if exclude_id is not None else 0)]
    filtro = _filtro(lang_filter, kind_filter)
    if filtro:
        args += ["FILTER", filtro]
    try:
        raw = idx.r.execute_command(*args)
    except Exception as exc:
        if "key does not exist" in str(exc).lower() or not idx.r.exists(idx.k("vec")):
            return []
        raise
    coppie = raw.items() if isinstance(raw, dict) else zip(raw[::2], raw[1::2])
    distanze = {int(el): 2.0 - 2.0 * float(score) for el, score in coppie}
    distanze.pop(exclude_id, None)
    righe = get_chunks(idx, list(distanze))
    out = [{**c, "distance": distanze[c["id"]]} for c in righe]
    return sorted(out, key=lambda x: x["distance"])[:limit]


def get_chunks(idx: Index, ids: list[int]) -> list[dict]:
    """I chunk ancora pubblicati fra `ids` (quelli tolti nel frattempo mancano)."""
    if not ids:
        return []
    out = []
    for cid, raw in zip(ids, idx.r.hmget(idx.k("chunks"), [str(i) for i in ids])):
        if raw is not None:
            out.append({"id": cid, **json.loads(raw)})
    return out


def _scan(idx: Index, name: str, match: str = "*") -> Iterable[tuple[str, bytes]]:
    for field, value in idx.r.hscan_iter(idx.k(name), match=match, count=1000):
        yield _s(field), value


def file_chunk_ids(idx: Index, kind: Optional[str] = None) -> dict:
    """(kind, path) → [id] dei file che hanno chunk pubblicati."""
    out = {}
    for field, value in _scan(idx, "filechunks", f"{kind}:*" if kind else "*"):
        out[split_fkey(field)] = json.loads(value)["ids"]
    return out


def list_chunks(idx: Index, kind: Optional[str] = None, limit: Optional[int] = None) -> list[dict]:
    ids = [i for lst in file_chunk_ids(idx, kind).values() for i in lst]
    ids.sort()
    return get_chunks(idx, ids[:limit] if limit else ids)


def list_wiki_pages(idx: Index) -> list[dict]:
    """Le pagine wiki indicizzate (slug + path + hash) per i controlli di coerenza."""
    pagine = [{"id": c["id"], "slug": c.get("func_name"), "file_path": c["file_path"], "content_sha": c.get("content_sha"),
               "page_type": c.get("lang"), "last_modified": c.get("last_modified")}
              for c in list_chunks(idx, "wiki")]
    return sorted(pagine, key=lambda p: p["slug"] or "")


def get_embedding_by_source(idx: Index, source: str, kind: Optional[str] = None) -> Optional[dict]:
    """Il primo chunk per file_path o slug (wiki)."""
    for k in ([kind] if kind else list(KINDS)):
        ids = file_chunk_ids(idx, k).get((k, source))
        if ids:
            return get_chunks(idx, [min(ids)])[0]
    for c in list_chunks(idx, kind):
        if c.get("func_name") == source:
            return c
    return None


def get_embedding_vector(idx: Index, chunk_id: int) -> Optional[list[float]]:
    """Il vettore di un chunk (dequantizzato da Q8) da usare come query k-NN."""
    try:
        v = idx.r.execute_command("VEMB", idx.k("vec"), str(chunk_id))
    except Exception:
        return None
    return [float(x) for x in v] if v else None


def manifest(idx: Index) -> dict:
    return {split_fkey(f): json.loads(v) for f, v in _scan(idx, "files")}


def get_run(idx: Index, kind: str) -> Optional[dict]:
    raw = idx.r.get(idx.k("run:" + kind))
    return json.loads(raw) if raw else None


def set_run(idx: Index, kind: str, run: dict, pipe=None) -> None:
    (pipe or idx.r).set(idx.k("run:" + kind), json.dumps(run))


def stats(idx: Index) -> dict:
    by_lang, by_kind = {}, {}
    for field, value in _scan(idx, "filechunks"):
        kind, _path = split_fkey(field)
        dati = json.loads(value)
        n = len(dati["ids"])
        by_kind[kind] = by_kind.get(kind, 0) + n
        by_lang[dati.get("lang") or ""] = by_lang.get(dati.get("lang") or "", 0) + n
    meta = idx.meta()
    try:
        mb = sum(idx.r.memory_usage(idx.k(n)) or 0 for n in ("vec", "chunks", "files", "filechunks")) / 1024 ** 2
    except Exception:
        mb = None
    return {
        "total_chunks": sum(by_kind.values()),
        "by_lang": dict(sorted(by_lang.items(), key=lambda x: -x[1])),
        "by_kind": by_kind,
        "last_modified": meta.get("last_indexed_at"),
        "embed_dim": meta.get("embed_dim"),
        "embed_provider": meta.get("embed_provider"),
        "embed_model": meta.get("embed_model"),
        "last_indexed_sha": meta.get("last_indexed_sha"),
        "redis_vectors": int(idx.r.execute_command("VCARD", idx.k("vec")) or 0) if idx.r.exists(idx.k("vec")) else 0,
        "redis_mb": round(mb, 1) if mb is not None else None,
    }


def prendi_lock(idx: Index, cosa: str, durata: int = 6 * 3600) -> Optional[str]:
    """Lock del progetto per le chiavi :new:* (ricostruzione, import). Un lock rimasto a un
    processo morto (crash durante una ricostruzione) si riprende. Torna il valore o None."""
    valore = f"{os.getpid()}:{cosa}"
    for _ in range(2):
        if idx.r.set(idx.k("lock"), valore, nx=True, ex=durata):
            return valore
        chi = _s(idx.r.get(idx.k("lock"))) or ""
        try:
            os.kill(int(chi.split(":")[0]), 0)
            return None
        except PermissionError:
            return None
        except (ValueError, ProcessLookupError):
            idx.r.delete(idx.k("lock"))
    return None


def lascia_lock(idx: Index, valore: str) -> None:
    if _s(idx.r.get(idx.k("lock"))) == valore:
        idx.r.delete(idx.k("lock"))


# --- import del vecchio formato -----------------------------------------------

def _legacy_keys(r, db_file: Path) -> list[str]:
    return ["raidhodev:vec:" + hashlib.sha1(str(p).encode()).hexdigest()[:16] for p in dict.fromkeys((db_file, db_file.resolve()))]


def _togli_legacy(r, db_file: Path) -> None:
    for k in _legacy_keys(r, db_file):
        r.delete(k, k + ":versione", k + ":lock", k + ":nuova")
    for p in (db_file, db_file.with_name(db_file.name + "-journal"), db_file.with_name(db_file.name + "-wal"),
              db_file.with_name(db_file.name + "-shm"), db_file.with_name(db_file.name + "?mode=ro")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def importa_legacy(idx: Index) -> dict:
    """Porta il vecchio code-index.db su Redis senza nuovi embedding: chunk, manifest e meta
    dal file (SQLite semplice, niente sqlite-vec), vettori dalla sua copia su Redis se era
    allineata. Se non lo era il file resta dov'e' e l'indice si ricostruisce al prossimo reindex."""
    db_file = idx.directory / CODE_DB_FILENAME
    if not db_file.is_file() or db_file.is_symlink():
        return {"import": False}
    r = idx.r
    if idx.built():
        _togli_legacy(r, db_file)
        return {"import": False, "legacy_removed": True}
    db = sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)
    try:
        tables = {t for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"meta", "chunks", "indexed_files"} <= tables:
            return {"import": False, "reason": "legacy schema"}
        meta = dict(db.execute("SELECT key, value FROM meta"))
        chunks = db.execute("SELECT id, file_path, func_name, line_start, line_end, content, lang, last_modified, "
                            "content_sha, kind FROM chunks").fetchall()
        files = db.execute("SELECT kind, file_path, snapshot FROM indexed_files").fetchall()
    finally:
        db.close()
    versione = f"{meta.get('index_fingerprint') or ''}|{meta.get('index_generation') or '0'}"
    vecchia = next((k for k in _legacy_keys(r, db_file)
                    if r.exists(k) and _s(r.get(k + ":versione")) == versione), None)
    if not vecchia or not meta.get("index_fingerprint") or int(r.execute_command("VCARD", vecchia) or 0) != len(chunks):
        return {"import": False, "reason": "redis copy not aligned"}
    lock = prendi_lock(idx, "import", 900)
    if lock is None:
        return {"import": False, "reason": "busy"}
    try:
        nuovo = idx.k("new")
        r.delete(*(f"{nuovo}:{n}" for n in ("vec", "chunks", "files", "filechunks")))
        rev = {(k, p): json.loads(s).get("hash") for k, p, s in files}
        per_file = {}
        pipe = r.pipeline(transaction=False)
        for n, (cid, path, func, ls, le, content, lang, mod, sha, kind) in enumerate(chunks, 1):
            pipe.hset(f"{nuovo}:chunks", str(cid), json.dumps({
                "file_path": path, "func_name": func, "line_start": ls, "line_end": le, "content": content,
                "lang": lang, "kind": kind, "last_modified": mod, "content_sha": sha, "rev": rev.get((kind, path))}))
            per_file.setdefault((kind, path), {"ids": [], "lang": lang})["ids"].append(cid)
            if n % 1000 == 0:
                pipe.execute()
        for (kind, path), dati in per_file.items():
            pipe.hset(f"{nuovo}:filechunks", fkey(kind, path), json.dumps(dati))
        for kind, path, snap in files:
            pipe.hset(f"{nuovo}:files", fkey(kind, path), snap)
        pipe.execute()
        # i vettori ci sono gia': si sposta il set e si aggiungono gli attributi per FILTER
        r.rename(vecchia, f"{nuovo}:vec")
        pipe = r.pipeline(transaction=False)
        for n, (cid, _path, _func, _ls, _le, _content, lang, _mod, _sha, kind) in enumerate(chunks, 1):
            pipe.execute_command("VSETATTR", f"{nuovo}:vec", str(cid), attributi({"kind": kind, "lang": lang}))
            if n % 2000 == 0:
                pipe.execute()
        pipe.execute()
        pipe = r.pipeline(transaction=True)
        for n in ("vec", "chunks", "files", "filechunks"):
            if r.exists(f"{nuovo}:{n}"):
                pipe.rename(f"{nuovo}:{n}", idx.k(n))
        pipe.delete(idx.k("meta"))
        pipe.hset(idx.k("meta"), mapping={**{k: v for k, v in meta.items() if v is not None},
                                           "imported_from_sqlite": str(len(chunks))})
        pipe.set(idx.k("nextid"), max((c[0] for c in chunks), default=0))
        pipe.execute()
        _togli_legacy(r, db_file)
        return {"import": True, "chunks": len(chunks), "files": len(files)}
    finally:
        lascia_lock(idx, lock)
