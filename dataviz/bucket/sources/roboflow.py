"""
Roboflow (Universe and private workspaces) through the REST API.

    refs     workspace/project/version[/format], https://universe.roboflow.com/<ws>/<project>[/dataset|model]/<N>,
             https://app.roboflow.com/<ws>/<project>/<N>
    token    a `roboflow` connection (api_key), required: Universe downloads need a (free) account key too
    inspect  GET https://api.roboflow.com/{ws}/{project}?api_key=  -> project name, type, images, classes, versions
    download GET https://api.roboflow.com/{ws}/{project}/{version}/{format}?api_key=  answers {export: {link}} once
             the export exists; until then Roboflow generates it ({progress}), so the same URL is polled every
             POLL seconds (at most WAIT), then the zip is downloaded and unpacked.
"""

import time
from urllib.parse import urlsplit

from ..store import BucketError
from . import fetch
from .base import Source, need_token, slug_name

API = 'https://api.roboflow.com'
POLL = 2.0
WAIT = 15 * 60
FORMATS = (('yolov11', 'YOLOv11'), ('yolov8', 'YOLOv8'), ('yolov5pytorch', 'YOLOv5'), ('yolov8-obb', 'YOLOv8 OBB'),
           ('coco', 'COCO JSON'), ('coco-segmentation', 'COCO segmentation'), ('voc', 'Pascal VOC'),
           ('createml', 'CreateML'), ('darknet', 'Darknet'), ('tfrecord', 'TFRecord'), ('folder', 'Folders (classify)'),
           ('png-mask-semantic', 'Semantic masks'))


class _Roboflow(Source):
    key = 'roboflow'
    label = 'Roboflow'
    needs = 'roboflow'
    token = 'required'
    examples = ('my-workspace/my-project/3', 'https://universe.roboflow.com/ws/project/dataset/2')
    help = 'workspace/project/version, or a Universe link. Exported by Roboflow in the format you pick.'
    fields = ({'key': 'format', 'label': 'Format', 'type': 'select', 'options': FORMATS, 'default': 'yolov8'},
              {'key': 'version', 'label': 'Version', 'type': 'text', 'placeholder': 'from the ref'})

    def parse_ref(self, ref):
        raw = str(ref or '').strip()
        fmt = None
        if '://' in raw or raw.startswith(('universe.roboflow.com', 'app.roboflow.com')):
            u = urlsplit(raw if '://' in raw else 'https://' + raw)
            if not (u.hostname or '').endswith('roboflow.com'):
                raise BucketError('that is not a roboflow.com link')
            parts = [p for p in u.path.split('/') if p]
            parts = [p for p in parts if p not in ('dataset', 'model', 'browse', 'images', 'generate')]
        else:
            parts = [p for p in raw.split('/') if p]
        if len(parts) >= 4:
            fmt = parts[3]
        if len(parts) < 2:
            raise BucketError(f'cannot read {ref!r}: use workspace/project/version or a Roboflow link')
        ws, proj = parts[0], parts[1]
        ver = parts[2] if len(parts) >= 3 else None
        if ver is not None and not ver.isdigit():
            raise BucketError(f'cannot read {ref!r}: the version is a number (workspace/project/3)')
        out = {'workspace': ws, 'project': proj, 'version': int(ver) if ver else None, 'format': fmt,
               'name': slug_name(proj, f'v{ver}' if ver else '')}
        out['ref'] = f'{ws}/{proj}' + (f'/{ver}' if ver else '')
        return out

    @staticmethod
    def _ver(ref, opts):
        v = (opts or {}).get('version') or ref.get('version')
        if not v or not str(v).isdigit():
            return None
        return int(v)

    def inspect(self, conn, ref, opts):
        key = need_token(self, conn, 'its API')
        with self.client() as c:
            d = fetch.get_json(c, f'{API}/{ref["workspace"]}/{ref["project"]}', params={'api_key': key},
                               errors={401: _bad(), 403: _bad(),
                                       404: f'no Roboflow project {ref["workspace"]}/{ref["project"]}'})
        p = d.get('project') or {}
        vers = [{'version': _vnum(v.get('id')), 'name': v.get('name'), 'images': v.get('images'),
                 'date': v.get('created')} for v in (d.get('versions') or [])]
        return {'title': p.get('name') or ref['project'], 'kind': p.get('type'), 'size': None,
                'files': p.get('images'), 'license': p.get('license'), 'versions': vers,
                'version': self._ver(ref, opts) or (vers[0]['version'] if vers else None),
                'classes': list((p.get('classes') or {}).keys())[:50] if isinstance(p.get('classes'), dict) else None,
                'formats': [f for f, _ in FORMATS],
                'url': f'https://universe.roboflow.com/{ref["workspace"]}/{ref["project"]}'}

    def download(self, conn, ref, opts, dest, progress, cancelled):
        key = need_token(self, conn, 'downloads')
        ver = self._ver(ref, opts)
        if ver is None:
            info = self.inspect(conn, ref, opts)
            ver = info.get('version')
            if not ver:
                raise BucketError(f'{ref["ref"]} has no version to export: generate one on roboflow.com', 404)
        fmt = (opts or {}).get('format') or ref.get('format') or 'yolov8'
        url = f'{API}/{ref["workspace"]}/{ref["project"]}/{ver}/{fmt}'
        t0 = time.time()
        with self.client() as c:
            while True:
                if cancelled is not None and cancelled():
                    raise fetch.Cancelled()
                d = fetch.get_json(c, url, params={'api_key': key},
                                   errors={401: _bad(), 403: _bad(),
                                           404: f'Roboflow has no version {ver} of {ref["workspace"]}/{ref["project"]} '
                                                f'in {fmt}'})
                link = (d.get('export') or {}).get('link') if isinstance(d.get('export'), dict) else None
                if link:
                    break
                if time.time() - t0 > WAIT:
                    raise BucketError(f'Roboflow did not finish the {fmt} export in {WAIT // 60} minutes', 504)
                pr = d.get('progress')
                if progress:
                    progress(None, None, 'Roboflow is preparing the export',
                             extra=f'{float(pr) * 100:.0f}%' if isinstance(pr, (int, float)) else '')
                time.sleep(POLL)
            res = fetch.download(link, dest, None, progress, cancelled, client=c,
                                 filename=f'{slug_name(ref["project"])}-v{ver}-{fmt}.zip')
        return {'files': [res['path']], 'bytes': res['bytes'], 'meta': {'version': ver, 'format': fmt}}


def _vnum(vid):
    s = str(vid or '').rsplit('/', 1)[-1]
    return int(s) if s.isdigit() else s or None


def _bad():
    return 'Roboflow refused the API key: check it in Settings › Connections (roboflow.com › Settings › API keys)'


Roboflow = _Roboflow()
