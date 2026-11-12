"""
Hugging Face Hub datasets (huggingface_hub, already a dependency).

    refs     user/dataset, user/dataset@revision, https://huggingface.co/datasets/user/dataset[/tree/<rev>[/<dir>]]
    token    optional `huggingface` connection (gated or private datasets)
    inspect  HfApi.dataset_info (license, sha) + list_repo_tree(recursive) for the file count and size
    download list_repo_tree, filtered by the patterns option (globs; a /tree/<rev>/<dir> link = <dir>/**), then
             hf_hub_download per file in an 8-thread pool into the import folder: cancellable between files, progress
             per file in bytes; hf_xet / hf_transfer are used by huggingface_hub when they are installed.

hf_api() and hf_download() are the only two calls into huggingface_hub (tests monkeypatch them).
"""

import fnmatch
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from ..store import BucketError
from . import fetch
from .base import Source, slug_name

WORKERS = 8
_ID = r'[A-Za-z0-9][A-Za-z0-9_.-]*'


def hf_api(token=None):
    from huggingface_hub import HfApi
    return HfApi(token=token or None)


def hf_download(**kw):
    from huggingface_hub import hf_hub_download
    return hf_hub_download(**kw)


def _token(conn):
    return ((conn or {}).get('api_key') or '').strip() or None


def _hf_error(e, repo):
    name = type(e).__name__
    if 'GatedRepo' in name:
        return BucketError(f'{repo} is gated: accept its terms on huggingface.co and use a token of that account', 403)
    if 'RepositoryNotFound' in name or 'RevisionNotFound' in name or 'EntryNotFound' in name:
        return BucketError(f'no Hugging Face dataset {repo} (or that revision; a private one needs a token)', 404)
    if isinstance(e, BucketError):
        return e
    return BucketError(f'Hugging Face: {e}', 502)


class _HF(Source):
    key = 'huggingface'
    label = 'Hugging Face'
    needs = 'huggingface'
    token = 'optional'
    examples = ('detection-datasets/coco', 'user/dataset@main', 'https://huggingface.co/datasets/user/dataset')
    help = 'A dataset repo id or link. A token is only needed for gated or private datasets.'
    fields = ({'key': 'revision', 'label': 'Revision', 'type': 'text', 'placeholder': 'main'},
              {'key': 'patterns', 'label': 'Only files matching', 'type': 'list', 'placeholder': 'data/*.parquet, *.json'})

    def parse_ref(self, ref):
        raw = str(ref or '').strip()
        rev, sub = None, ''
        if '://' in raw or raw.startswith('huggingface.co') or raw.startswith('hf.co'):
            u = urlsplit(raw if '://' in raw else 'https://' + raw)
            if (u.hostname or '') not in ('huggingface.co', 'www.huggingface.co', 'hf.co'):
                raise BucketError('that is not a huggingface.co link')
            parts = [p for p in u.path.split('/') if p]
            if parts[:1] != ['datasets'] or len(parts) < 3:
                raise BucketError('use a dataset link: https://huggingface.co/datasets/<user>/<name>')
            raw = f'{parts[1]}/{parts[2]}'
            if len(parts) >= 5 and parts[3] in ('tree', 'blob', 'resolve'):
                rev, sub = parts[4], '/'.join(parts[5:])
        if raw.startswith('datasets/'):
            raw = raw[len('datasets/'):]
        if '@' in raw:
            raw, rev = raw.split('@', 1)
        if not re.fullmatch(fr'{_ID}/{_ID}', raw):
            raise BucketError(f'cannot read {ref!r}: use user/dataset or a huggingface.co/datasets link')
        return {'repo': raw, 'revision': rev or None, 'subdir': sub, 'name': slug_name(raw.split('/')[1]),
                'ref': raw + (f'@{rev}' if rev else '')}

    def _files(self, conn, ref, opts):
        rev = (opts or {}).get('revision') or ref.get('revision') or None
        pats = list((opts or {}).get('patterns') or [])
        if ref.get('subdir') and not pats:
            pats = [ref['subdir'].rstrip('/') + '/**']
        try:
            api = hf_api(_token(conn))
            tree = list(api.list_repo_tree(ref['repo'], repo_type='dataset', revision=rev, recursive=True))
        except Exception as e:  # noqa: BLE001 - huggingface_hub raises its own error classes
            raise _hf_error(e, ref['repo']) from None
        files = []
        for t in tree:
            size = getattr(t, 'size', None)
            path = getattr(t, 'path', None) or getattr(t, 'rfilename', None)
            if path is None or size is None and type(t).__name__ == 'RepoFolder':
                continue
            if type(t).__name__ == 'RepoFolder' or path in ('.gitattributes',) or path.startswith('.'):
                continue
            if pats and not any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path, p.replace('/**', '/*'))
                                or (p.endswith('/**') and path.startswith(p[:-2])) for p in pats):
                continue
            files.append((path, int(size or 0)))
        return api, rev, files

    def inspect(self, conn, ref, opts):
        api, rev, files = self._files(conn, ref, opts)
        lic, sha = None, None
        try:
            info = api.dataset_info(ref['repo'], revision=rev)
            sha = getattr(info, 'sha', None)
            cd = getattr(info, 'card_data', None) or getattr(info, 'cardData', None)
            lic = (cd.get('license') if hasattr(cd, 'get') else getattr(cd, 'license', None)) if cd else None
            if not lic:
                lic = next((t.split(':', 1)[1] for t in (getattr(info, 'tags', None) or [])
                            if str(t).startswith('license:')), None)
        except Exception:  # noqa: BLE001 - the tree already proved the repo exists
            pass
        if isinstance(lic, list):
            lic = ', '.join(map(str, lic))
        return {'title': ref['repo'], 'size': sum(s for _, s in files), 'files': len(files), 'license': lic,
                'versions': [], 'revision': rev or 'main', 'sha': sha, 'sample': [p for p, _ in files[:20]],
                'url': f'https://huggingface.co/datasets/{ref["repo"]}'}

    def download(self, conn, ref, opts, dest, progress, cancelled):
        _, rev, files = self._files(conn, ref, opts)
        if not files:
            raise BucketError(f'{ref["repo"]} has no files' + (' matching those patterns' if (opts or {}).get('patterns')
                                                           else ''), 404)
        total = sum(s for _, s in files)
        fetch.check_disk(dest, total)
        meter = fetch.Meter(progress, total)
        tok = _token(conn)

        def one(item):
            path, size = item
            if cancelled is not None and cancelled():
                raise fetch.Cancelled()
            try:
                p = hf_download(repo_id=ref['repo'], filename=path, repo_type='dataset', revision=rev, token=tok,
                                local_dir=dest)
            except Exception as e:  # noqa: BLE001
                raise _hf_error(e, ref['repo']) from None
            meter.add(size, force=True)
            return str(p)

        out = []
        with ThreadPoolExecutor(WORKERS) as ex:
            futs = [ex.submit(one, f) for f in files]
            try:
                for f in futs:
                    out.append(f.result())
            except BaseException:
                for f in futs:
                    f.cancel()
                raise
        shutil.rmtree(os.path.join(dest, '.cache'), ignore_errors=True)    # hf_hub_download's local_dir metadata
        return {'files': out, 'bytes': total}


HuggingFace = _HF()
