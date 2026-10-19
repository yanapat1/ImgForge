"""
HTTP plumbing for the page: gzip for JSON / text (not for JPEGs, already compressed) and static files
that browsers must revalidate (no stale page code after an edit; unchanged files cost a 304).
"""

import hashlib
import json
import re
from urllib.parse import parse_qs

from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware


class JsonGZip:
    """gzip API JSON and static text (big win over a port-forward), but not JPEGs (already compressed)."""

    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1024)

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '') if scope['type'] == 'http' else ''
        if path.startswith(('/api/crop/', '/api/image/', '/api/cover/')):
            return await self.app(scope, receive, send)
        return await self.gzip(scope, receive, send)


NO_CACHE = {'Cache-Control': 'no-cache'}   # browser may keep a copy but must revalidate (cheap 304 via ETag)


IMMUTABLE = {'Cache-Control': 'public, max-age=31536000, immutable'}


class NoCacheStatic(StaticFiles):
    """Page code is revalidated (cheap 304 via ETag) so an edit shows up; a URL with ?h=<content hash> never changes
    (the page shell stamps every file with its hash), so the browser keeps those without asking: over an SSH tunnel
    the next page load costs no round trip per file."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if resp.status_code == 200 and 'h' in parse_qs(scope.get('query_string', b'').decode()):
            resp.headers.update(IMMUTABLE)
        elif resp.status_code in (200, 304):
            resp.headers.update(NO_CACHE)   # without it the browser guesses a lifetime from Last-Modified and serves stale CSS
        return resp


_hash_memo = {}     # path -> (mtime_ns, size, hash)
_IMPORT = re.compile(r"""(?:^|[\s;])(?:import|export)\s[^'"]*?from\s*['"](\.[^'"]+)['"]|(?:^|[\s;])import\s*['"](\.[^'"]+)['"]""",
                     re.M)


def _file_hash(path):
    st = path.stat()
    hit = _hash_memo.get(path)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    h = hashlib.sha1(path.read_bytes()).hexdigest()[:10]
    _hash_memo[path] = (st.st_mtime_ns, st.st_size, h)
    return h


def _static_imports(path, root):
    """Modules `path` imports statically (relative specifiers only), as paths under `root`."""
    out = []
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return out
    for m in _IMPORT.finditer(text):
        spec = m.group(1) or m.group(2)
        target = (path.parent / spec).resolve()
        if target.is_file() and root in target.parents:
            out.append(target)
    return out


def render_index(static_dir, prefix=''):
    """index.html with every static URL stamped ?h=<content hash>, an import map so all modules (nested imports
    included) are fetched under their hashed URL, and <link rel=modulepreload> for the whole static import graph of
    main.js, so the browser fetches the ~40 modules in parallel instead of one import level per round trip.

    prefix: put in front of every static URL (stamped src / href, import map keys and values, preloads, the icon
    sprite's <use href>): '../../' for the visualizer under /s/<id>/, so every session shares one browser cache of
    /static/ instead of downloading the page code again per dataset. '' for the home and login pages."""
    root = static_dir.resolve()
    html = (root / 'index.html').read_text(encoding='utf-8')

    def stamp(m):
        f = root / m.group(2).removeprefix('static/')     # the URL is static/<rel>, the file is <static_dir>/<rel>
        return f'{m.group(1)}{prefix}{m.group(2)}?h={_file_hash(f)}"' if f.is_file() else m.group(0)
    html = re.sub(r'((?:src|href)=")(static/[^"?]+)\?v=[^"]*"', stamp, html)
    if prefix:
        html = html.replace('<use href="static/', f'<use href="{prefix}static/')

    entry = root / 'js' / 'main.js'
    if not entry.is_file():
        return html
    base = f'{prefix}static/' if prefix else './static/'
    imap, seen, order = {}, {entry}, [entry]
    for js in root.glob('js/**/*.js'):
        rel = js.relative_to(root).as_posix()
        imap[f'{base}{rel}'] = f'{base}{rel}?h={_file_hash(js)}'
    for mod in order:                                   # breadth-first over static imports
        for dep in _static_imports(mod, root):
            if dep not in seen:
                seen.add(dep)
                order.append(dep)
    preload = ''.join(f'<link rel="modulepreload" href="{prefix}static/{m.relative_to(root).as_posix()}?h={_file_hash(m)}">\n'
                      for m in order if m != entry)
    head = f'<script type="importmap">{json.dumps({"imports": imap})}</script>\n{preload}'
    marker = f'<script src="{prefix}static/js/theme-boot.js'
    return html.replace(marker, head + marker, 1) if marker in html else html
