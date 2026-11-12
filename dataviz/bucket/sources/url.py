"""
Any direct http(s) link: an archive (unpacked into the bucket) or a single file (kept as it is).

    refs     https://… (GitHub blob links -> raw; GitHub release assets as they are; Google Drive share links ->
             drive.usercontent.google.com/download?id=…&export=download&confirm=t; Dropbox ?dl=0 -> ?dl=1)
    token    none
    inspect  HEAD (a GET that is closed at once when HEAD is refused): file name, size, type
    download fetch.download: resumable, 8 ranged connections for big files when the server accepts ranges
"""

import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx

from ..store import BucketError
from . import fetch
from .base import Source, slug_name

ARCHIVE = ('.zip', '.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz', '.gz', '.7z')


def rewrite(url):
    """The direct-download form of a share link (GitHub blob, Google Drive, Dropbox); others unchanged."""
    u = urlsplit(url)
    host = (u.hostname or '').lower()
    if host == 'github.com':
        m = re.fullmatch(r'/([^/]+)/([^/]+)/blob/(.+)', u.path)
        if m:
            return f'https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}'
    if host in ('drive.google.com', 'docs.google.com'):
        m = re.search(r'/file/d/([A-Za-z0-9_-]+)', u.path)
        fid = m.group(1) if m else (parse_qs(u.query).get('id') or [None])[0]
        if fid:
            return f'https://drive.usercontent.google.com/download?{urlencode({"id": fid, "export": "download", "confirm": "t"})}'
    if host.endswith('dropbox.com'):
        q = parse_qs(u.query)
        q['dl'] = ['1']
        return urlunsplit((u.scheme, u.netloc, u.path, urlencode({k: v[0] for k, v in q.items()}), ''))
    return url


def is_archive(name):
    return str(name).lower().endswith(ARCHIVE)


class _Url(Source):
    key = 'url'
    label = 'Link (URL)'
    examples = ('https://github.com/ultralytics/assets/releases/download/v0.0.0/coco128.zip',
                'https://drive.google.com/file/d/<id>/view')
    help = 'A direct link to a file or an archive (zip, tar.gz, …). Google Drive, Dropbox and GitHub links work too.'

    def parse_ref(self, ref):
        raw = str(ref or '').strip()
        if not re.match(r'^https?://', raw, re.I):
            raise BucketError('paste an http(s) link')
        url = rewrite(raw)
        fetch.check_url(url)
        name = fetch.safe_name(urlsplit(url).path.rsplit('/', 1)[-1], 'download')
        stem = re.sub(r'(\.tar)?\.[A-Za-z0-9]{1,4}$', '', name) or 'download'
        return {'url': url, 'name': slug_name(stem), 'ref': raw}

    def inspect(self, conn, ref, opts):
        with self.client() as c:
            try:
                resp, final = fetch.request(c, 'HEAD', ref['url'])
                if resp.status_code in (403, 405, 501) or resp.status_code >= 500:
                    resp, final = fetch.request(c, 'GET', ref['url'], stream=True)
                    resp.close()
            except httpx.HTTPError as e:
                raise BucketError(f'{urlsplit(ref["url"]).hostname}: {e}', 502) from None
        if resp.status_code >= 400:
            raise BucketError(f'{urlsplit(final).hostname} answered {resp.status_code}',
                          resp.status_code if resp.status_code in (401, 403, 404) else 502)
        name = fetch.filename_of(resp, final)
        size = int(resp.headers.get('content-length') or 0) or None
        ctype = (resp.headers.get('content-type') or '').split(';')[0].strip()
        if 'text/html' in ctype and not name.lower().endswith(('.htm', '.html')):
            note = 'the link answers a web page, not a file: use the file’s direct download link'
        else:
            note = None
        return {'title': name, 'size': size, 'files': 1, 'license': None, 'versions': [], 'type': ctype or None,
                'archive': is_archive(name), 'ranges': resp.headers.get('accept-ranges', '').lower() == 'bytes',
                'url': final, **({'warning': note} if note else {})}

    def download(self, conn, ref, opts, dest, progress, cancelled):
        res = fetch.download(ref['url'], dest, None, progress, cancelled)
        return {'files': [res['path']], 'bytes': res['bytes']}


Url = _Url()
