"""
OpenML datasets (tabular).

    refs     61, https://www.openml.org/d/61, https://www.openml.org/search?type=data&id=61
    token    none (public data)
    inspect  GET https://www.openml.org/api/v1/json/data/{id} -> name, version, licence, format, target, rows
             (data/qualities/{id}: NumberOfInstances / NumberOfFeatures, best effort)
    download the parquet file (parquet_url) when OpenML has one, else the ARFF (url), plus description.json with the
             dataset's description (name, version, licence, target, citation, original url)
"""

import json
import os
import re

from ..store import BucketError
from . import fetch
from .base import Source, slug_name

API = 'https://www.openml.org/api/v1/json'


class _OpenML(Source):
    key = 'openml'
    label = 'OpenML'
    examples = ('61', 'https://www.openml.org/d/61')
    help = 'A dataset id or its openml.org link. Downloads the table (Parquet, else ARFF) and its description.'
    fields = ({'key': 'format', 'label': 'File', 'type': 'select', 'default': 'parquet',
               'options': (('parquet', 'Parquet (when available)'), ('arff', 'ARFF'))},)

    def parse_ref(self, ref):
        raw = str(ref or '').strip()
        m = (re.fullmatch(r'\d+', raw) or re.search(r'openml\.org/(?:d|api/v1/json/data)/(\d+)', raw)
             or re.search(r'openml\.org/search\?.*\bid=(\d+)', raw))
        if not m:
            raise BucketError(f'cannot read {ref!r}: use an OpenML dataset id or its openml.org link')
        did = m.group(1) if m.groups() else m.group(0)
        return {'id': did, 'name': f'openml-{did}', 'ref': did}

    def _desc(self, c, ref):
        d = fetch.get_json(c, f'{API}/data/{ref["id"]}', errors={404: f'no OpenML dataset {ref["id"]}',
                                                                 412: f'no OpenML dataset {ref["id"]}'})
        return d.get('data_set_description') or {}

    def inspect(self, conn, ref, opts):
        with self.client() as c:
            d = self._desc(c, ref)
            rows = cols = None
            try:
                q = fetch.get_json(c, f'{API}/data/qualities/{ref["id"]}')
                qs = {x.get('name'): x.get('value') for x in (q.get('data_qualities') or {}).get('quality') or []}
                rows = int(float(qs['NumberOfInstances'])) if qs.get('NumberOfInstances') else None
                cols = int(float(qs['NumberOfFeatures'])) if qs.get('NumberOfFeatures') else None
            except (BucketError, ValueError, KeyError, AttributeError):
                pass
        return {'title': f'{d.get("name") or ref["id"]} (v{d.get("version")})' if d.get('version') else d.get('name') or ref['id'],
                'size': None, 'files': 2, 'license': d.get('licence'), 'versions': [], 'version': d.get('version'),
                'rows': rows, 'columns': cols, 'target': d.get('default_target_attribute'),
                'parquet': bool(d.get('parquet_url')), 'format': d.get('format'),
                'url': f'https://www.openml.org/d/{ref["id"]}'}

    def download(self, conn, ref, opts, dest, progress, cancelled):
        want = (opts or {}).get('format') or 'parquet'
        with self.client() as c:
            d = self._desc(c, ref)
            name = slug_name(d.get('name') or f'openml-{ref["id"]}')
            pq = d.get('parquet_url')
            if want == 'parquet' and pq:
                url, fn = pq, f'{name}.parquet'
            elif d.get('url'):
                url, fn = d['url'], f'{name}.arff'
            else:
                raise BucketError(f'OpenML dataset {ref["id"]} has no downloadable file', 404)
            res = fetch.download(url, dest, None, progress, cancelled, client=c, filename=fn, disk_factor=1.2)
        keep = ('id', 'name', 'version', 'description', 'format', 'licence', 'default_target_attribute', 'citation',
                'original_data_url', 'creator', 'collection_date', 'upload_date', 'md5_checksum')
        desc = os.path.join(dest, 'description.json')
        with open(desc, 'w', encoding='utf-8') as f:
            json.dump({k: d[k] for k in keep if k in d}, f, indent=2, ensure_ascii=False)
        return {'files': [res['path'], desc], 'bytes': res['bytes'],
                'meta': {'name': d.get('name'), 'version': d.get('version')}}


OpenML = _OpenML()
