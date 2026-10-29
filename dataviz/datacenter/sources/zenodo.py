"""
Zenodo records (research datasets with a DOI).

    refs     1234567, 10.5281/zenodo.1234567, https://doi.org/10.5281/zenodo.1234567,
             https://zenodo.org/records/1234567 (or /record/)
    token    optional `zenodo` connection (restricted records you have access to): Authorization: Bearer
    inspect  GET https://zenodo.org/api/records/{id} -> title, license, version, files [{key, size, checksum}]
    download every file (or the ones matching the patterns option) one after the other through fetch.download,
             each checked against its md5 checksum; archives are unpacked by the service afterwards.
"""

import fnmatch
import hashlib
import re
from urllib.parse import quote, urlsplit

from ..store import DCError
from . import fetch
from .base import Source, slug_name

API = 'https://zenodo.org/api'


def _headers(conn):
    key = (conn or {}).get('api_key') or ''
    return {'Authorization': f'Bearer {key.strip()}'} if key.strip() else {}


def md5_of(path):
    h = hashlib.md5()                                            # noqa: S324 - Zenodo's own checksum, not security
    with open(path, 'rb') as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


class _Zenodo(Source):
    key = 'zenodo'
    label = 'Zenodo'
    needs = 'zenodo'
    token = 'optional'
    examples = ('10.5281/zenodo.1234567', 'https://zenodo.org/records/1234567')
    help = 'A record id, its DOI or its link. A token is only needed for restricted records.'
    fields = ({'key': 'patterns', 'label': 'Only files matching', 'type': 'list', 'placeholder': '*.zip'},)

    def parse_ref(self, ref):
        raw = str(ref or '').strip()
        m = (re.fullmatch(r'\d+', raw) or re.search(r'zenodo\.(\d+)', raw)
             or re.search(r'zenodo\.org/(?:api/)?records?/(\d+)', raw))
        if not m:
            raise DCError(f'cannot read {ref!r}: use a Zenodo record id, its DOI (10.5281/zenodo.N) or its link')
        rid = m.group(1) if m.groups() else m.group(0)
        return {'id': rid, 'name': f'zenodo-{rid}', 'ref': f'10.5281/zenodo.{rid}'}

    def _record(self, c, conn, ref):
        return fetch.get_json(c, f'{API}/records/{ref["id"]}', _headers(conn),
                              errors={401: 'Zenodo refused the token (401): check it in Settings › Connections',
                                      403: 'this Zenodo record is restricted: add a token that has access',
                                      404: f'no Zenodo record {ref["id"]}', 410: f'Zenodo record {ref["id"]} was removed'})

    @staticmethod
    def _files(d, opts):
        pats = list((opts or {}).get('patterns') or [])
        out = []
        for f in d.get('files') or []:
            name = f.get('key') or f.get('filename') or ''
            link = (f.get('links') or {}).get('self') or (f.get('links') or {}).get('download')
            if not name or not link:
                continue
            if pats and not any(fnmatch.fnmatch(name, p) for p in pats):
                continue
            ck = str(f.get('checksum') or '')
            out.append({'name': name, 'size': int(f.get('size') or f.get('filesize') or 0), 'url': link,
                        'md5': ck.split(':', 1)[1] if ck.startswith('md5:') else None})
        return out

    def inspect(self, conn, ref, opts):
        with self.client() as c:
            d = self._record(c, conn, ref)
        md = d.get('metadata') or {}
        lic = md.get('license')
        files = self._files(d, opts)
        return {'title': md.get('title') or d.get('title') or ref['ref'], 'size': sum(f['size'] for f in files),
                'files': len(files), 'license': (lic.get('id') if isinstance(lic, dict) else lic),
                'versions': [], 'version': md.get('version'), 'doi': d.get('doi') or md.get('doi'),
                'sample': [f['name'] for f in files[:20]], 'url': f'https://zenodo.org/records/{ref["id"]}'}

    def download(self, conn, ref, opts, dest, progress, cancelled):
        h = _headers(conn)
        with self.client() as c:
            d = self._record(c, conn, ref)
            files = self._files(d, opts)
            if not files:
                raise DCError(f'Zenodo record {ref["id"]} has no files' +
                              (' matching those patterns' if (opts or {}).get('patterns') else ''), 404)
            total = sum(f['size'] for f in files)
            fetch.check_disk(dest, total)
            meter = fetch.Meter(progress, total)
            out = []
            for f in files:
                res = fetch.download(f['url'], dest, h, None, cancelled, client=c, filename=f['name'],
                                     size_hint=f['size'], meter=meter, disk_factor=0)
                if f['md5'] and md5_of(res['path']) != f['md5']:
                    raise DCError(f'{f["name"]} arrived damaged (its md5 does not match Zenodo’s): try again', 502)
                out.append(res['path'])
        return {'files': out, 'bytes': total, 'meta': {'title': (d.get('metadata') or {}).get('title'),
                                                       'host': urlsplit(API).hostname, 'record': quote(ref['id'])}}


Zenodo = _Zenodo()
