"""RAI-625: .html/.vue in chunk, submodule tracciati, ignore e bundle fuori."""
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


def project_with_submodule(tmp_path):
    origin = tmp_path / 'origin'
    init(origin)
    write(origin, 'pkg/lib.py', 'def answer():\n    return 42\n')
    write(origin, '.gitignore', 'pkg/local_secret.py\n')
    git(origin, 'add', 'pkg/lib.py', '.gitignore')
    git(origin, 'commit', '-q', '-m', 'lib')
    sha = git(origin, 'rev-parse', 'HEAD')

    project = tmp_path / 'project'
    init(project)
    write(project, 'app.py', 'import pkg\n')
    subprocess.run(['git', '-C', str(project), 'clone', '-q', str(origin), 'mods/lib'], check=True)
    git(project, 'update-index', '--add', '--cacheinfo', f'160000,{sha},mods/lib')
    return project


def test_html_e_vue_scoperti_e_in_chunk(tmp_path):
    assert code_index.LANG_BY_EXT['.html'] == 'html'
    assert code_index.LANG_BY_EXT['.vue'] == 'vue'
    write(tmp_path, 'templates/pagina.html', '<section>\n  <h1>titolo</h1>\n</section>\n')
    write(tmp_path, 'src/Pannello.vue', '<template>\n  <p>pannello</p>\n</template>\n')
    files, _excluded = index_policy.discover(tmp_path)
    found = {str(p.relative_to(tmp_path)): p for p in files}
    assert set(found) == {'templates/pagina.html', 'src/Pannello.vue'}
    for rel, marker in (('templates/pagina.html', 'titolo'), ('src/Pannello.vue', 'pannello')):
        chunks = code_index.chunk_file(found[rel])
        assert chunks, rel
        assert any(marker in c['content'] for c in chunks)


def test_file_dei_submodule_tracciati(tmp_path):
    project = project_with_submodule(tmp_path)
    loose = project / 'extra' / 'loose'
    init(loose)
    write(loose, 'no.py', 'x = 1\n')

    files, excluded = index_policy.discover(project)
    rels = {str(p.relative_to(project)) for p in files}
    assert 'mods/lib/pkg/lib.py' in rels
    assert 'app.py' in rels
    assert 'extra/loose/no.py' not in rels
    reasons = {e['path']: e['reason'] for e in excluded}
    assert reasons['extra/loose'] == 'default_directory'


def test_ignore_dentro_il_submodule(tmp_path):
    project = project_with_submodule(tmp_path)
    write(project, 'mods/lib/pkg/local_secret.py', 'secret = 1\n')
    write(project, 'mods/lib/pkg/drop_me.py', 'drop = 1\n')
    write(project, 'mods/lib/credentials.json', '{}\n')
    write(project, 'mods/lib/.env', 'TOKEN=1\n')
    write(project, 'mods/lib/server.pem', '-----BEGIN-----\n')
    write(project, '.raidhoignore', 'mods/lib/pkg/drop_me.py\n')

    files, excluded = index_policy.discover(project)
    rels = {str(p.relative_to(project)) for p in files}
    assert 'mods/lib/pkg/lib.py' in rels
    assert 'mods/lib/pkg/local_secret.py' not in rels
    assert 'mods/lib/pkg/drop_me.py' not in rels
    assert 'mods/lib/credentials.json' not in rels
    assert 'mods/lib/.env' not in rels
    assert 'mods/lib/server.pem' not in rels
    reasons = {e['path']: e['reason'] for e in excluded}
    assert reasons['mods/lib/pkg/local_secret.py'] == 'gitignore'
    assert reasons['mods/lib/pkg/drop_me.py'] == 'raidhoignore'
    assert reasons['mods/lib/credentials.json'] == 'sensitive_or_hidden'
    assert reasons['mods/lib/.env'] == 'sensitive_or_hidden'
    assert reasons['mods/lib/server.pem'] == 'sensitive_or_hidden'


def test_bundle_generati_esclusi(tmp_path):
    write(tmp_path, 'static/app.min.js', 'var a=1;\n')
    write(tmp_path, 'static/app.min.mjs', 'var b=1;\n')
    write(tmp_path, 'static/lib-min.js', 'var c=1;\n')
    write(tmp_path, 'static/app.bundle.js', 'var d=1;\n')
    write(tmp_path, 'static/app.js', 'var e = 1;\n')
    files, excluded = index_policy.discover(tmp_path)
    assert {str(p.relative_to(tmp_path)) for p in files} == {'static/app.js'}
    reasons = {e['path']: e['reason'] for e in excluded}
    for name in ('static/app.min.js', 'static/app.min.mjs', 'static/lib-min.js', 'static/app.bundle.js'):
        assert reasons[name] == 'generated'


def test_raidhoignore_negazione_e_archive_fuori_da_git(tmp_path):
    write(tmp_path, '.raidhoignore', '/archive/\nprivate/*.py\n!private/keep.py\n')
    write(tmp_path, 'archive/vecchio.py', 'x = 1\n')
    write(tmp_path, 'private/no.py', 'x = 1\n')
    write(tmp_path, 'private/keep.py', 'y = 1\n')
    write(tmp_path, 'ok.py', 'z = 1\n')
    files, excluded = index_policy.discover(tmp_path)
    assert {str(p.relative_to(tmp_path)) for p in files} == {'private/keep.py', 'ok.py'}
    reasons = {e['path']: e['reason'] for e in excluded}
    assert reasons['archive/vecchio.py'] == 'raidhoignore'
    assert reasons['private/no.py'] == 'raidhoignore'
