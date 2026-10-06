"""RAI-625: .html/.vue in chunk, submodule tracciati dentro, archive/ e bundle fuori."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import code_index  # noqa: E402
import index_policy  # noqa: E402


def write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def git(cwd, *args):
    result = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(path)], check=True)
    git(path, 'config', 'user.email', 't@example.com')
    git(path, 'config', 'user.name', 't')


def test_html_e_vue_scoperti_e_in_chunk(tmp_path):
    html = '<section>\n  <h1>titolo</h1>\n</section>\n'
    vue = '<template>\n  <p>pannello</p>\n</template>\n'
    write(tmp_path, 'templates/pagina.html', html)
    write(tmp_path, 'src/Pannello.vue', vue)
    files, _excluded = index_policy.discover(tmp_path)
    found = {str(p.relative_to(tmp_path)): p for p in files}
    assert set(found) == {'templates/pagina.html', 'src/Pannello.vue'}
    for rel, marker in (('templates/pagina.html', 'titolo'), ('src/Pannello.vue', 'pannello')):
        chunks = code_index.chunk_file(found[rel])
        assert chunks, rel
        assert any(marker in c['content'] for c in chunks)


def test_file_dei_submodule_tracciati(tmp_path):
    origin = tmp_path / 'origin'
    init(origin)
    write(origin, 'pkg/lib.py', 'def answer():\n    return 42\n')
    git(origin, 'add', 'pkg/lib.py')
    git(origin, 'commit', '-q', '-m', 'lib')
    sha = git(origin, 'rev-parse', 'HEAD')

    project = tmp_path / 'project'
    init(project)
    write(project, 'app.py', 'import pkg\n')
    subprocess.run(['git', '-C', str(project), 'clone', '-q', str(origin), 'mods/lib'], check=True)
    git(project, 'update-index', '--add', '--cacheinfo', f'160000,{sha},mods/lib')
    loose = project / 'vendor' / 'loose'
    init(loose)
    write(loose, 'no.py', 'x = 1\n')

    files, _excluded = index_policy.discover(project)
    rels = {str(p.relative_to(project)) for p in files}
    assert 'mods/lib/pkg/lib.py' in rels
    assert 'app.py' in rels
    assert 'vendor/loose/no.py' not in rels


def test_archive_e_bundle_esclusi(tmp_path):
    write(tmp_path, '.raidhoignore', '/archive/\n')
    write(tmp_path, 'archive/vecchio.py', 'x = 1\n')
    write(tmp_path, 'static/app.min.js', 'var a=1;\n')
    write(tmp_path, 'static/app.bundle.js', 'var b=1;\n')
    write(tmp_path, 'static/app.js', 'var c = 1;\n')
    files, excluded = index_policy.discover(tmp_path)
    assert {str(p.relative_to(tmp_path)) for p in files} == {'static/app.js'}
    reasons = {e['path']: e['reason'] for e in excluded}
    assert reasons['archive/vecchio.py'] == 'raidhoignore'
    assert reasons['static/app.min.js'] == 'generated'
    assert reasons['static/app.bundle.js'] == 'generated'
