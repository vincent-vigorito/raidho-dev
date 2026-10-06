"""RAI-534: tetto in caratteri sui chunk dell'indice semantico (righe lunghissime spezzate per colonna)."""
import hashlib
import json
import os
import random
import string
import subprocess
import sys
from pathlib import Path

import pytest
from _helpers import cov_env
from test_embed_mock import redis_usable

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
PYTHON = os.environ.get("RAIDHO_TEST_PYTHON", sys.executable)
sys.path.insert(0, str(SCRIPTS))
import code_index  # noqa: E402
import search_evidence  # noqa: E402

CAP = code_index.MAX_CHUNK_CHARS
OLD_KEYS = ("func_name", "line_start", "line_end", "content")


def noise(n, seed):
    rnd = random.Random(seed)
    return "".join(rnd.choice(string.ascii_letters + string.digits + " ;(){}=+.,") for _ in range(n))


def line_offsets(text):
    offsets, pos = [], 0
    for line in text.split("\n"):
        offsets.append(pos)
        pos += len(line) + 1
    return offsets


def span(text, chunk):
    """Intervallo [inizio, fine) del chunk nel testo: col_start sulla prima riga, col_end sull'ultima
    (0-based, fine esclusa); senza colonne il chunk copre righe intere."""
    lines = text.split("\n")
    offsets = line_offsets(text)
    first, last = chunk["line_start"], chunk["line_end"]
    col_start, col_end = chunk.get("col_start"), chunk.get("col_end")
    start = offsets[first - 1] + (col_start if col_start is not None else 0)
    end = offsets[last - 1] + (col_end if col_end is not None else len(lines[last - 1]))
    return start, end


def assert_exact_cover(text, chunks):
    """Ogni chunk e' esattamente la sua porzione di testo e i chunk insieme coprono tutto il testo
    (a parte gli a capo e le righe vuote, che il chunker scarta da sempre)."""
    covered = bytearray(len(text))
    for chunk in chunks:
        start, end = span(text, chunk)
        assert text[start:end] == chunk["content"], (chunk["line_start"], chunk.get("col_start"), chunk.get("col_end"))
        covered[start:end] = b"\x01" * (end - start)
    lost = [i for i, c in enumerate(text) if not covered[i] and not c.isspace()]
    assert not lost, f"{len(lost)} caratteri persi, primo a offset {lost[0]}"


def assert_capped(chunks):
    too_big = [(c["line_start"], len(c["content"])) for c in chunks if len(c["content"]) > CAP]
    assert not too_big, too_big


# --- 1. righe oltre il tetto -------------------------------------------------------------------

def test_long_lines_split_by_column():
    lines = [noise(150_000, seed) for seed in range(3)]
    text = "\n".join(lines) + "\n"
    chunks = code_index.chunk_text(text, ".js")
    assert chunks, "file saltato"
    assert_capped(chunks)
    for n in (1, 2, 3):
        pieces = sorted((c for c in chunks if c["line_start"] == n), key=lambda c: c["col_start"])
        assert len(pieces) > 1, f"riga {n} non spezzata"
        assert all(c["line_end"] == n for c in pieces)
        assert pieces[0]["col_start"] == 0
        assert pieces[-1]["col_end"] == 150_000
        for prev, cur in zip(pieces, pieces[1:]):
            assert cur["col_start"] == prev["col_end"], "pezzi non contigui"
        assert "".join(c["content"] for c in pieces) == lines[n - 1]
    assert_exact_cover(text.removesuffix("\n"), chunks)


@pytest.mark.parametrize("extra,expected", [(0, 1), (1, 2)])
def test_line_at_cap_boundary(extra, expected):
    line = noise(CAP + extra, 7)
    chunks = code_index.chunk_text(line, ".txt")
    assert len(chunks) == expected
    assert_capped(chunks)
    assert_exact_cover(line, chunks)


def test_multibyte_long_line():
    line = ("àèìòù€😀" * 40_000)[:250_000]
    chunks = code_index.chunk_text(line, ".txt")
    assert len(chunks) > 1
    assert_capped(chunks)
    assert_exact_cover(line, chunks)


def test_window_of_short_lines_over_cap():
    """80 righe ognuna sotto il tetto ma insieme sopra: anche la finestra a righe resta sotto il tetto."""
    width = CAP // 2 + 1
    count = min(80, 450_000 // (width + 1))
    assert count >= 3
    text = "\n".join(noise(width, i) for i in range(count))
    chunks = code_index.chunk_text(text, ".txt")
    assert_capped(chunks)
    assert_exact_cover(text, chunks)


@pytest.mark.parametrize("text", ["", "   \n\n\t\n"])
def test_empty_text_has_no_chunks(text):
    assert code_index.chunk_text(text, ".js") == []


# --- 2. copertura completa ---------------------------------------------------------------------

def test_cover_normal_file():
    text = "\n".join(f"riga {i} " + noise(30, i) for i in range(1, 251))
    chunks = code_index.chunk_text(text, ".txt")
    assert_exact_cover(text, chunks)


@pytest.mark.parametrize("suffix", [".txt", ".py", ".js"])
def test_cover_long_lines_mixed_with_short(suffix):
    parts = []
    for i in range(120):
        if i in (5, 6, 60, 119):
            parts.append("x = '" + noise(70_000, i) + "'")
        else:
            parts.append(f"y{i} = {i}")
    text = "\n".join(parts)
    chunks = code_index.chunk_text(text + "\n", suffix)
    assert_capped(chunks)
    assert_exact_cover(text, chunks)
    assert any(c.get("col_start") is not None for c in chunks)


# --- 3. regressione: file normali come prima --------------------------------------------------

def old_fields(chunks):
    return [tuple(c[k] for k in OLD_KEYS) for c in chunks]


def test_regression_python_file():
    lines = ["import os", "", "X = 1", ""]
    lines += ["def big():"] + [f"    v{i} = {i}" for i in range(1, 100)]  # righe 5..104
    lines += [""]
    lines += ["class C:"] + [f"    a{i} = {i}" for i in range(1, 10)]  # righe 106..115
    text = "\n".join(lines) + "\n"

    def body(a, b):
        return "\n".join(lines[a - 1:b])
    expected = [("<module>", 1, 4, body(1, 4)),
                ("big", 5, 84, body(5, 84)),
                ("big", 75, 104, body(75, 104)),
                ("C", 106, 115, body(106, 115))]
    chunks = code_index.chunk_text(text, ".py")
    assert old_fields(chunks) == expected
    assert all(c.get("col_start") is None and c.get("col_end") is None for c in chunks)


def test_regression_text_file():
    lines = [f"line {i}" for i in range(1, 201)]
    text = "\n".join(lines) + "\n"

    def body(a, b):
        return "\n".join(lines[a - 1:b])
    expected = [(None, 1, 80, body(1, 80)), (None, 71, 150, body(71, 150)), (None, 141, 200, body(141, 200))]
    chunks = code_index.chunk_text(text, ".txt")
    assert old_fields(chunks) == expected
    assert all(c.get("col_start") is None and c.get("col_end") is None for c in chunks)


def test_regression_short_file_single_chunk():
    text = "a\nb\nc\n"
    assert old_fields(code_index.chunk_text(text, ".txt")) == [(None, 1, 3, "a\nb\nc")]


# --- 4. file minificati ----------------------------------------------------------------------

def minified_js(seed):
    """Come un bundle minificato: una riga enorme di funzioni concatenate e qualche riga corta."""
    rnd = random.Random(seed)
    parts, size = [], 0
    while size < 260_000:
        name = "".join(rnd.choice(string.ascii_letters) for _ in range(rnd.randint(1, 3)))
        part = f"var {name}=function(t,e){{return t&&e?{name}(t-1,e)+'{noise(rnd.randint(5, 60), size)}':null}};"
        parts.append(part)
        size += len(part)
    return "/*! bundle v1 */\n" + "".join(parts) + "\n" + "!function(){" + noise(40_000, seed) + "}();\n"


@pytest.mark.parametrize("seed", [1, 2])
def test_minified_files(tmp_path, seed):
    path = tmp_path / "bundle.min.js"
    path.write_text(minified_js(seed), encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    chunks = code_index.chunk_file(path)
    assert len(chunks) > 1
    assert_capped(chunks)
    assert_exact_cover(text.removesuffix("\n"), chunks)
    assert any(c["line_start"] == 2 and c.get("col_start") is not None for c in chunks)


# --- 4b. evidenze di code.search sui pezzi di riga ---------------------------------------------

def column_hit(root, col_start, col_end, content, **extra):
    return {"level": 2, "results": [{"path": "app.min.js", "line_start": 2, "line_end": 2, "preview": content,
                                     "col_start": col_start, "col_end": col_end, "_indexed_content": content,
                                     "_indexed_revision": hashlib.sha256((root / "app.min.js").read_bytes()).hexdigest(),
                                     **extra}]}


def write_min(root):
    line = noise(30_000, 3)
    (root / "app.min.js").write_text("// header\n" + line + "\n")
    return line


def test_evidence_accepts_column_piece(tmp_path):
    line = write_min(tmp_path)
    piece = line[12_000:24_000]
    checked = search_evidence.finalize(column_hit(tmp_path, 12_000, 24_000, piece), tmp_path, piece[100:120], 100_000)
    assert checked["count"] == 1, checked["evidence"]
    evidence = checked["results"][0]["evidence"]
    assert evidence["spans"] == [{"line_start": 2, "line_end": 2, "col_start": 12_000, "col_end": 24_000}]
    assert evidence["match"] == "literal"


def test_evidence_rejects_tampered_column_piece(tmp_path):
    line = write_min(tmp_path)
    tampered = line[:12_000] + "#" + line[12_001:]
    (tmp_path / "app.min.js").write_text("// header\n" + tampered + "\n")
    hit = column_hit(tmp_path, 12_000, 24_000, line[12_000:24_000])
    assert search_evidence.finalize(hit, tmp_path, "x", 100)["evidence"]["rejected"] == [
        {"reason": "index_content_mismatch"}]


@pytest.mark.parametrize("cols,lines", [((5, 5), (2, 2)), ((9, 3), (2, 2)), ((0, 10), (1, 2))])
def test_evidence_rejects_invalid_columns(tmp_path, cols, lines):
    line = write_min(tmp_path)
    hit = column_hit(tmp_path, *cols, line[cols[0]:cols[1]])
    hit["results"][0]["line_start"], hit["results"][0]["line_end"] = lines
    assert search_evidence.finalize(hit, tmp_path, "x", 100)["evidence"]["rejected"] == [{"reason": "invalid_lines"}]


# --- 5. pipeline end-to-end con provider mock --------------------------------------------------

PIPELINE = r'''
import sys, json, os, atexit
from pathlib import Path
sys.path.insert(0, sys.argv[1])
os.environ['RAIDHO_EMBED_PROVIDER'] = 'mock'
import code_db, code_index, embed_providers, index_pipeline as pipeline
root = Path(sys.argv[2])
_r = code_db._redis()
_base = code_db.base_key(root / '.raidhowiki')
atexit.register(lambda: [_r.delete(k) for k in _r.scan_iter(_base + '*')])
(root / '.raidhowiki/wiki').mkdir(parents=True)
lines = ['var a' + str(i) + '="' + ('q%dw ' % i) * 30_000 + '";' for i in range(3)]
(root / 'long.js').write_text('\n'.join(lines))
(root / 'small.py').write_text('def f():\n    return 1\n')
cap = code_index.MAX_CHUNK_CHARS
batch_cap = getattr(pipeline, 'MAX_BATCH_CHARS', None)
batches = []
class Strict(embed_providers.MockProvider):
    def embed(self, texts):
        # come il provider reale: un input oltre il limite del modello fa fallire la richiesta
        assert all(len(t) <= cap for t in texts), 'input oltre il tetto'
        batches.append(sum(len(t) for t in texts))
        return super().embed(texts)
p = Strict()
pipeline.embed_providers.get_provider = lambda: p
r = pipeline.refresh(root, kind='code')
assert r['status'] == 'ready', r
idx = code_db.open_db(root / '.raidhowiki', dim=p.dim, allow_dimension_mismatch=True)
stored = [json.loads(raw) for _f, raw in _r.hscan_iter(idx.k('chunks'), count=1000)]
long_chunks = [c for c in stored if c['file_path'] == 'long.js']
assert len(long_chunks) > 3, len(long_chunks)
assert all(c.get('col_start') is not None and c.get('col_end') is not None for c in long_chunks), long_chunks[0].keys()
for n, line in enumerate(lines, 1):
    pieces = sorted((c for c in long_chunks if c['line_start'] == n), key=lambda c: c['col_start'])
    assert ''.join(c['content'] for c in pieces) == line, n
    assert all(line[c['col_start']:c['col_end']] == c['content'] for c in pieces)
assert any(c['file_path'] == 'small.py' for c in stored)
print(json.dumps({'batches': batches, 'batch_cap': batch_cap, 'cap': cap}))
'''


@pytest.mark.skipif(not redis_usable(), reason="Redis with vector sets unavailable")
def test_pipeline_indexes_long_lines(tmp_path):
    result = subprocess.run([PYTHON, "-c", PIPELINE, str(SCRIPTS), str(tmp_path)], capture_output=True, text=True,
                            timeout=120, env={**os.environ, **cov_env()})
    assert result.returncode == 0, result.stdout + result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["batches"]
    if out["batch_cap"] is not None:
        assert out["cap"] <= out["batch_cap"]
        assert max(out["batches"]) <= out["batch_cap"], out
        assert len(out["batches"]) > 1, "tutti i chunk in un'unica richiesta"
