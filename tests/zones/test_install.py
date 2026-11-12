"""An analyst install: Bucket + Data with the visualizer's files left out (`python -m dataviz.zones prune`,
the Docker image's ZONES build argument). The kept code must never import a pruned file, and a pruned copy must start
and serve its pages with the visualizer's libraries absent."""

import ast
import json
import os
import posixpath
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from conftest import TEST_SECRET_KEY
from test_zones import APP_DIR, SERVE, VISUALIZER_LIBS

ANALYST = 'core,bucket,data'
PASSWORD = 'zone-test-password-1'
TS = APP_DIR / 'static' / 'ts'
_STATIC_IMPORT = re.compile(r"^\s*import\s+(?!type\b)(?:[^'\"]*?from\s+)?['\"](\.[^'\"]+)['\"]", re.M)


@pytest.fixture
def analyst(monkeypatch):
    monkeypatch.setenv('DATAVIZ_ZONES', ANALYST)
    from dataviz import zones
    return {p for p in zones.prunable()}


def _gone(rel, pruned):
    return any(rel == p or (p.endswith('/') and rel.startswith(p)) for p in pruned)


def test_every_listed_path_exists(analyst):
    missing = [p for p in analyst if not p.startswith('static/js/') and not (APP_DIR / p).exists()]
    assert not missing, f'dataviz/zones.py FILES names paths that are not in the repo: {missing}'


def test_full_install_prunes_nothing(monkeypatch):
    monkeypatch.setenv('DATAVIZ_ZONES', '')
    from dataviz import zones
    assert zones.prunable() == []


def test_kept_frontend_never_imports_a_pruned_file(analyst):
    """A static import of a missing module breaks every page, so kept code reaches the visualizer only through
    core/hooks.ts or a zone-guarded dynamic import()."""
    bad = []
    for f in TS.rglob('*.ts'):
        rel = f.relative_to(APP_DIR).as_posix()
        if f.name.endswith('.d.ts') or _gone(rel, analyst):
            continue
        for spec in _STATIC_IMPORT.findall(f.read_text(encoding='utf-8')):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec))
            target = target[:-3] + '.ts' if target.endswith('.js') else target
            if _gone(target, analyst):
                bad.append(f'{rel} -> {target}')
    assert not bad, bad


def _load_time_imports(path, module):
    """The dataviz modules a file imports when it loads: module level and class bodies, not inside functions."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    pkg = module if path.name == '__init__.py' else module.rsplit('.', 1)[0]
    out, todo = [], list(tree.body)
    while todo:
        n = todo.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(n, ast.ImportFrom):
            base = pkg
            for _ in range(n.level - 1):
                base = base.rsplit('.', 1)[0]
            tgt = (f'{base}.{n.module}' if n.module else base) if n.level else (n.module or '')
            out += [f'{tgt}.{a.name}' for a in n.names] + [tgt]
        elif isinstance(n, ast.Import):
            out += [a.name for a in n.names]
        else:
            todo.extend(ast.iter_child_nodes(n))
    return [m for m in out if m.startswith('dataviz')]


def test_kept_backend_loads_nothing_pruned(analyst):
    def rel_of(mod):
        for cand in (mod.replace('.', '/') + '.py', mod.replace('.', '/') + '/__init__.py'):
            if (APP_DIR / cand).exists():
                return cand
        return None
    bad = []
    for f in (APP_DIR / 'dataviz').rglob('*.py'):
        rel = f.relative_to(APP_DIR).as_posix()
        if '__pycache__' in rel or '/migrations/' in rel or _gone(rel, analyst):
            continue
        mod = rel[:-3].replace('/', '.').removesuffix('.__init__')
        for dep in _load_time_imports(f, mod):
            target = rel_of(dep)
            if target and _gone(target, analyst):
                bad.append(f'{rel} -> {target}')
    assert not bad, bad


def test_prune_removes_the_files_and_their_build(tmp_path, analyst):
    from dataviz import zones
    for rel in ('dataviz/state.py', 'dataviz/ml/features.py', 'static/ts/plot/render.ts', 'static/js/plot/render.js',
                'static/js/plot/render.js.map', 'static/js/views/gallery.js', 'dataviz/data/service.py',
                'static/js/data/app.js', 'static/js/main.js'):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text('x')
    gone = zones.prune(tmp_path)
    assert 'dataviz/state.py' in gone and 'dataviz/ml/' in gone
    assert not (tmp_path / 'dataviz/ml').exists() and not (tmp_path / 'static/js/plot').exists()
    assert not (tmp_path / 'static/js/views/gallery.js').exists()
    for kept in ('dataviz/data/service.py', 'static/js/data/app.js', 'static/js/main.js'):
        assert (tmp_path / kept).exists()


# ----------------------------------------------------------------------------------- a pruned copy, in a real browser
sync_api = pytest.importorskip('playwright.sync_api')


@pytest.fixture
def pruned_server(pg, tmp_path):
    if not (APP_DIR / 'static' / 'js' / 'viz' / 'boot.js').is_file():
        pytest.skip('the frontend is not built (npm run build)')
    app = tmp_path / 'app'
    skip = shutil.ignore_patterns('__pycache__', '*.pyc', 'node_modules')
    shutil.copytree(APP_DIR / 'dataviz', app / 'dataviz', ignore=skip)
    shutil.copytree(APP_DIR / 'static', app / 'static', ignore=skip)
    for f in ('app.py', 'config.yaml'):
        shutil.copy2(APP_DIR / f, app / f)
    env = {**os.environ, 'DATAVIZ_ZONES': ANALYST, 'PYTHONPATH': str(app)}
    out = subprocess.run([sys.executable, '-m', 'dataviz.zones', 'prune'], cwd=app, env=env, capture_output=True,
                         text=True, check=True).stdout
    assert 'removed' in out and not (app / 'dataviz' / 'state.py').exists() and not (app / 'static' / 'js' / 'plot').exists()

    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(json.dumps({'dataset': {'workspace_root': str(tmp_path / 'ws')}, 'workers': {'enabled': False},
                               'logging': {'level': 'WARNING'}}))
    env.update({'DATABASE_URL': pg, 'DATAVIZ_SECRET_KEY': TEST_SECRET_KEY, 'DATAVIZ_ADMIN_USERNAME': 'admin',
                'DATAVIZ_ADMIN_PASSWORD': PASSWORD, 'DATAVIZ_PROJECT_ROOT': str(tmp_path)})
    libs = [x for x in VISUALIZER_LIBS if x != 'scipy']          # Data uses scipy for its statistic tests
    proc = subprocess.Popen([sys.executable, '-c', SERVE, ','.join(libs), '--config', str(cfg), '--host', '127.0.0.1',
                             '--port', str(port)], cwd=app, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        for _ in range(120):
            if proc.poll() is not None:
                pytest.fail(f'the pruned server exited:\n{proc.stdout.read()[-3000:]}')
            try:
                urllib.request.urlopen(f'{base}/api/auth/config', timeout=5).read()
                break
            except OSError:
                time.sleep(0.5)
        else:
            pytest.fail('the pruned server did not answer in 60 s')
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_a_pruned_analyst_install_serves_its_pages(pruned_server):
    problems = []
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.on('pageerror', lambda e: problems.append(f'pageerror: {e}'))
        page.on('console', lambda m: problems.append(f'console: {m.text}') if m.type == 'error' else None)
        page.on('response', lambda r: problems.append(f'{r.status} {r.url}')
                if r.status >= 400 and '/api/auth/me' not in r.url else None)
        page.request.post(f'{pruned_server}/api/auth/login', data={'login': 'admin', 'password': PASSWORD},
                          headers={'X-Requested-With': 'dataviz'})
        page.goto(pruned_server)                                    # Home of an analyst install: Bucket and Data, in that order
        page.wait_for_selector('#door-data .door-recent, #door-data .door-empty', timeout=20000)
        assert [x.strip() for x in page.locator('.door h2').all_inner_texts()] == ['Bucket', 'Data']
        page.goto(f'{pruned_server}/?bucket')
        page.wait_for_selector('.bk-header', timeout=20000)
        html = page.content()
        assert '<main class="viz-only">' not in html, re.findall(r'.{0,120}<main class="viz-only">', html)[:1]
        assert 'curation.css' not in html and 'compare.css' not in html
        page.click('.bk-header [data-act="settings"]')            # Settings opens without the visualizer's markup
        page.wait_for_selector('.snav[data-sec="connections"].active', timeout=10000)
        page.keyboard.press('Escape')
        page.goto(f'{pruned_server}/?data')
        page.wait_for_selector('.dt-main', timeout=20000)
        page.wait_for_selector('#dt-sql .cm-content', timeout=20000)
        page.wait_for_timeout(800)
        browser.close()
    assert not problems, problems
    for gone in ('static/js/viz/boot.js', 'static/js/plot/render.js', 'static/js/views/gallery.js'):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f'{pruned_server}/{gone}', timeout=5)
        assert e.value.code == 404
