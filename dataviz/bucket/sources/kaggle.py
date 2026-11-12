"""
Kaggle datasets and competitions (the public REST API, no `kaggle` package needed).

    refs     owner/dataset, owner/dataset/versions/N, https://www.kaggle.com/datasets/owner/dataset[/versions/N],
             competitions/<name>, https://www.kaggle.com/competitions/<name> (or /c/<name>)
    auth     a `kaggle` connection: username + api_key from kaggle.json -> HTTP Basic; a new-style KGAT_... API
             token (or a key without a username) -> Authorization: Bearer
    inspect  GET /api/v1/datasets/view/{owner}/{slug} (title, totalBytes, licenseName, versions) and
             GET /api/v1/datasets/list/{owner}/{slug} (files); a competition: GET /api/v1/competitions/data/list/{name}
    download GET /api/v1/datasets/download/{owner}/{slug}[?datasetVersionNumber=N] -> a redirect to a signed storage
             URL (the key is not forwarded there) -> one zip. A competition:
             GET /api/v1/competitions/data/download-all/{name}; 403 = the rules are not accepted yet.
"""

import base64
import os
import re
import zipfile
from urllib.parse import urlsplit

from ..store import BucketError
from . import fetch
from .base import Source, need_token, slug_name

API = 'https://www.kaggle.com/api/v1'
_PART = r'[A-Za-z0-9][A-Za-z0-9_.-]*'
RULES = ('Kaggle refused the download (403): accept the competition rules on kaggle.com first '
         '(open the competition, Data tab, “I understand and accept”), then try again')


def auth_headers(conn):
    key = need_token(Kaggle, conn, 'downloads')
    user = (conn.get('username') or '').strip()
    if key.startswith('KGAT_') or not user:
        return {'Authorization': f'Bearer {key}'}
    return {'Authorization': 'Basic ' + base64.b64encode(f'{user}:{key}'.encode()).decode()}


class _Kaggle(Source):
    key = 'kaggle'
    label = 'Kaggle'
    needs = 'kaggle'
    token = 'required'
    examples = ('ultralytics/coco128', 'owner/dataset/versions/3', 'competitions/titanic')
    help = 'A dataset (owner/dataset or its URL) or a competition (competitions/<name>). Uses your Kaggle token.'
    fields = ({'key': 'version', 'label': 'Version', 'type': 'text', 'placeholder': 'latest'},)

    def parse_ref(self, ref):
        raw = str(ref or '').strip()
        if raw.startswith('kaggle '):                 # a pasted CLI line: kaggle datasets download -d owner/slug
            m = re.search(r'(?:-d|-c|--dataset|--competition)\s+(\S+)', raw)
            if m:
                raw = ('competitions/' if re.search(r'\s-c\s|--competition', raw) else '') + m.group(1)
        if '://' in raw or raw.startswith('www.kaggle.com') or raw.startswith('kaggle.com'):
            u = urlsplit(raw if '://' in raw else 'https://' + raw)
            if not (u.hostname or '').endswith('kaggle.com'):
                raise BucketError('that is not a kaggle.com link')
            parts = [p for p in u.path.split('/') if p]
            if parts[:1] in (['c'], ['competitions']) and len(parts) >= 2:
                return self._comp(parts[1])
            if parts[:1] == ['datasets']:
                parts = parts[1:]
            raw = '/'.join(parts)
        parts = [p for p in raw.strip('/').split('/') if p]
        if len(parts) >= 2 and parts[0] in ('competitions', 'c'):
            return self._comp(parts[1])
        if len(parts) >= 2 and all(re.fullmatch(_PART, p) for p in parts[:2]):
            version = None
            if len(parts) >= 4 and parts[2] == 'versions' and parts[3].isdigit():
                version = int(parts[3])
            elif len(parts) > 2 and parts[2] not in ('data', 'code', 'discussion', 'suggestions', 'versions'):
                raise BucketError(f'cannot read {ref!r}: use owner/dataset or owner/dataset/versions/N')
            out = {'kind': 'dataset', 'owner': parts[0], 'slug': parts[1], 'version': version,
                   'name': slug_name(parts[1])}
            out['ref'] = f'{parts[0]}/{parts[1]}' + (f'/versions/{version}' if version else '')
            return out
        raise BucketError(f'cannot read {ref!r}: use owner/dataset, a kaggle.com dataset link or competitions/<name>')

    @staticmethod
    def _comp(name):
        if not re.fullmatch(_PART, name):
            raise BucketError(f'{name!r} is not a competition name')
        return {'kind': 'competition', 'competition': name, 'name': slug_name(name), 'ref': f'competitions/{name}'}

    @staticmethod
    def _version(ref, opts):
        v = (opts or {}).get('version') or ref.get('version')
        if v in (None, '', 'latest'):
            return None
        if not str(v).isdigit():
            raise BucketError('the Kaggle version is a number (or empty for the latest)')
        return int(v)

    def inspect(self, conn, ref, opts):
        h = auth_headers(conn)
        with self.client() as c:
            if ref['kind'] == 'competition':
                files = fetch.get_json(c, f'{API}/competitions/data/list/{ref["competition"]}', h,
                                       errors={401: _bad_token(), 403: RULES,
                                               404: f'no Kaggle competition {ref["competition"]!r}'})
                rows = files if isinstance(files, list) else (files.get('files') or [])
                return {'title': ref['competition'], 'kind': 'competition', 'files': len(rows),
                        'size': sum(int(f.get('totalBytes') or f.get('size') or 0) for f in rows) or None,
                        'license': None, 'versions': [], 'sample': [f.get('name') or f.get('ref') for f in rows[:20]],
                        'url': f'https://www.kaggle.com/competitions/{ref["competition"]}'}
            d = fetch.get_json(c, f'{API}/datasets/view/{ref["owner"]}/{ref["slug"]}', h,
                               errors={401: _bad_token(), 403: 'Kaggle refused (403): the dataset is private',
                                       404: f'no Kaggle dataset {ref["owner"]}/{ref["slug"]} (or it is private)'})
            try:
                fl = fetch.get_json(c, f'{API}/datasets/list/{ref["owner"]}/{ref["slug"]}', h)
                files = fl.get('datasetFiles') or [] if isinstance(fl, dict) else []
            except BucketError:
                files = []
        versions = [{'version': v.get('versionNumber'), 'date': v.get('creationDate'), 'notes': v.get('versionNotes')}
                    for v in (d.get('versions') or [])][:30]
        return {'title': d.get('title') or ref['slug'], 'subtitle': d.get('subtitle'), 'kind': 'dataset',
                'size': d.get('totalBytes'), 'files': len(files) or None, 'license': d.get('licenseName'),
                'versions': versions, 'version': self._version(ref, opts) or d.get('currentVersionNumber'),
                'sample': [f.get('name') for f in files[:20]],
                'url': f'https://www.kaggle.com/datasets/{ref["owner"]}/{ref["slug"]}'}

    def download(self, conn, ref, opts, dest, progress, cancelled):
        h = auth_headers(conn)
        if ref['kind'] == 'competition':
            url, name = f'{API}/competitions/data/download-all/{ref["competition"]}', f'{ref["competition"]}.zip'
        else:
            v = self._version(ref, opts)
            url = f'{API}/datasets/download/{ref["owner"]}/{ref["slug"]}' + (f'?datasetVersionNumber={v}' if v else '')
            name = f'{ref["slug"]}.zip'
        try:
            res = fetch.download(url, dest, h, progress, cancelled, filename=None)
        except BucketError as e:
            if e.status == 403:
                raise BucketError(RULES if ref['kind'] == 'competition'
                              else 'Kaggle refused the download (403): the dataset is private or needs its terms '
                                   'accepted on kaggle.com', 403) from None
            if e.status == 401:
                raise BucketError(_bad_token(), 401) from None
            if e.status == 404:
                raise BucketError(f'Kaggle has no {ref["ref"]} (or not that version)', 404) from None
            raise
        if not res['filename'].lower().endswith(('.zip', '.tar', '.gz', '.tgz', '.7z')) and zipfile.is_zipfile(res['path']):
            new = os.path.join(dest, name)
            os.replace(res['path'], new)
            res['path'] = new
        return {'files': [res['path']], 'bytes': res['bytes']}


def _bad_token():
    return 'Kaggle refused the token (401): check the username and key (Settings › Connections)'


Kaggle = _Kaggle()
