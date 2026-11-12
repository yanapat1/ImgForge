"""
An "embeddings" visualize session: vectors computed anywhere (audio, video, text, tabular, ...) plus a table of
labels and metadata, read in place from one folder.

    <folder>/meta.csv | meta.parquet      one row per sample: label?, id?, image? audio? video? text?, any other columns
    <folder>/<name>.npy                   (N, D) float vectors, row i = meta row i; every file is one feature space

`image` / `audio` / `video` are paths relative to the folder (or http(s) URLs); `text` is the text itself. Every
other column is metadata: numeric when every value parses as a number, else categorical (<= MAX_CATEGORIES distinct
values) or free text. Same small interface as sources.LocalSource where the State touches it (name, uri, root,
kind, fingerprint(), check(), list_labels(), list_images()), plus feature_registry() and build_index().
"""

import csv
import hashlib
import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from ..core import logging as vlog

META_NAMES = ('meta.csv', 'meta.parquet')
PREVIEW_COLS = ('image', 'audio', 'video', 'text')
RESERVED = ('label', 'id', *PREVIEW_COLS)
MAX_CATEGORIES = 50
MAX_META_ROWS = 2_000_000
_log = vlog.get_logger('index')

AUDIO_TYPES = {'.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.flac': 'audio/flac', '.ogg': 'audio/ogg',
               '.m4a': 'audio/mp4', '.aac': 'audio/aac', '.opus': 'audio/ogg', '.webm': 'audio/webm'}
VIDEO_TYPES = {'.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime', '.m4v': 'video/mp4',
               '.ogv': 'video/ogg', '.mkv': 'video/x-matroska'}
IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
               '.bmp': 'image/bmp', '.gif': 'image/gif'}


def is_url(ref):
    return ref.startswith(('http://', 'https://'))


def shown_previews(preview, allow_urls):
    """The preview kinds of a row the page may load: URL ones only when config embeddings.allow_url_previews is on."""
    return sorted(k for k, v in preview.items() if allow_urls or k == 'text' or not is_url(v))


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def column_types(rows, cols):
    """'num' | 'cat' | 'text' per metadata column. Blank cells are ignored (a column of blanks is 'cat')."""
    out = {}
    for c in cols:
        vals = [r.get(c, '') for r in rows]
        filled = [v for v in vals if v not in ('', None)]
        if filled and all(_num(v) is not None for v in filled):
            out[c] = 'num'
        else:
            out[c] = 'cat' if len(set(filled)) <= MAX_CATEGORIES else 'text'
    return out


class EmbeddingSource:
    kind = 'embeddings'

    def __init__(self, path, name=None):
        self.root = Path(path).expanduser().resolve()
        self.name = name or self.root.name
        self.uri = str(self.root)
        self._meta = None                  # (fingerprint, rows, columns)

    # ------------------------------------------------------------ files
    def meta_path(self):
        for n in META_NAMES:
            if (self.root / n).is_file():
                return self.root / n
        return None

    def vector_files(self):
        return sorted(p for p in self.root.glob('*.npy') if p.is_file())

    def fingerprint(self):
        """Changes when the table or any vector file is added, removed, rewritten or touched."""
        h = hashlib.sha1(b'embeddings-v1')
        for p in [self.meta_path(), *self.vector_files()]:
            if p is not None:
                st = p.stat()
                h.update(f'{p.name}|{st.st_size}|{st.st_mtime_ns}\n'.encode())
        return h.hexdigest()

    # same calls the State makes on an image source: an embeddings session has no label files or images
    def list_labels(self):
        return []

    def list_images(self):
        return []

    # ------------------------------------------------------------ reading
    def load_meta(self):
        """(rows: list of dicts of strings, columns in file order). Cached until the fingerprint changes."""
        fp = self.fingerprint()
        if self._meta and self._meta[0] == fp:
            return self._meta[1], self._meta[2]
        p = self.meta_path()
        if p is None:
            raise ValueError(f'{self.root} has no {" or ".join(META_NAMES)}')
        if p.suffix == '.parquet':
            try:
                import pyarrow.parquet as pq
            except ImportError:
                raise ValueError('meta.parquet needs pyarrow (pip install pyarrow), or save the table as meta.csv') from None
            table = pq.read_table(p)
            cols = list(table.column_names)
            data = table.to_pydict()
            rows = [{c: '' if data[c][i] is None else str(data[c][i]) for c in cols} for i in range(table.num_rows)]
        else:
            with open(p, newline='', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                cols = [c.strip() for c in reader.fieldnames or []]
                rows = []
                for i, r in enumerate(reader):
                    if i >= MAX_META_ROWS:
                        raise ValueError(f'{p.name} has more than {MAX_META_ROWS:,} rows')
                    rows.append({(k or '').strip(): (v or '').strip() for k, v in r.items() if k is not None})
        if not cols:
            raise ValueError(f'{p.name} has no header row')
        self._meta = (fp, rows, cols)
        return rows, cols

    def load_vectors(self, key):
        """(N, D) float32 for feature `key` (byo-<file stem>)."""
        p = self.root / f'{key.removeprefix("byo-")}.npy'
        x = np.load(p, mmap_mode='r', allow_pickle=False)
        if x.ndim != 2:
            raise ValueError(f'{p.name} must be a 2D array (rows × dims), not {x.shape}')
        return np.ascontiguousarray(x, dtype=np.float32)

    def _safe_ref(self, ref):
        """A preview path relative to the folder, refused when it leaves the folder (.. or a symlink out)."""
        if is_url(ref):
            return ref
        p = (self.root / ref).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f'preview path {ref!r} is outside {self.root}')
        return str(p.relative_to(self.root))

    def preview_path(self, ref):
        """Absolute path of a (validated) local preview ref."""
        return self.root / self._safe_ref(ref)

    def check(self):
        """Validate the folder; returns a summary for the session form. ValueError with a readable reason."""
        if not self.root.is_dir():
            raise ValueError(f'{self.root} is not a folder')
        rows, cols = self.load_meta()
        n = len(rows)
        if not n:
            raise ValueError('the metadata table has no rows')
        files = self.vector_files()
        if not files:
            raise ValueError(f'no .npy vector files next to {self.meta_path().name}')
        features = []
        for p in files:
            x = np.load(p, mmap_mode='r', allow_pickle=False)
            if x.ndim != 2 or x.shape[0] != n:
                raise ValueError(f'{p.name}: shape {x.shape}, expected ({n}, dims): one row per metadata row')
            if not np.issubdtype(x.dtype, np.number):
                raise ValueError(f'{p.name}: not a numeric array ({x.dtype})')
            features.append({'key': f'byo-{p.stem}', 'name': p.stem, 'dim': int(x.shape[1])})
        previews = [c for c in PREVIEW_COLS if c in cols]
        n_url = 0
        for c in previews:
            if c != 'text':
                for r in rows:
                    if r.get(c):
                        self._safe_ref(r[c])
                        n_url += is_url(r[c])
        meta_cols = [c for c in cols if c not in RESERVED]
        types = column_types(rows, meta_cols)
        labels = sorted({r.get('label', '') for r in rows}) if 'label' in cols else []
        return {'n': n, 'features': features, 'previews': previews, 'url_previews': n_url, 'label': 'label' in cols,
                'classes': len(labels), 'columns': [{'name': c, 'type': types[c]} for c in meta_cols]}

    # ------------------------------------------------------------ feature spaces
    def feature_registry(self):
        """One FeatureSpec per .npy file. Rows that are already unit length are compared by cosine, the rest by
        Euclidean distance (L2-normalizing them would throw away the magnitude the user computed)."""
        from ..ml import features as feats
        out = {}
        for p in self.vector_files():
            key = f'byo-{p.stem}'
            x = np.load(p, mmap_mode='r', allow_pickle=False)
            sample = np.asarray(x[:min(len(x), 2000)], np.float64) if x.ndim == 2 else np.zeros((0, 0))
            unit = sample.size and np.allclose(np.linalg.norm(sample, axis=1), 1.0, atol=1e-3)
            dim = x.shape[1] if x.ndim == 2 else 0

            def fn(_paths, _samples, progress, key=key, unit=unit):
                progress(stage=f'reading {key.removeprefix("byo-")}.npy')
                v = self.load_vectors(key)
                if not np.isfinite(v).all():
                    raise ValueError(f'{key.removeprefix("byo-")}.npy has NaN / inf values')
                return feats.l2_normalize(v) if unit else v

            out[key] = feats.FeatureSpec(key=key, label=p.stem, group='provided', fn=fn,
                                         metric='cosine' if unit else 'euclidean',
                                         note=f'{dim} dims from {p.name}' + (' (unit length: cosine)' if unit else ''))
        return out

    # ------------------------------------------------------------ index
    def build_index(self, cache_dir, thumb_size=224, rebuild=False, progress=None):
        """samples.json in the same shape as indexer.build_index; image previews become crops/<id>.jpg thumbnails,
        so every <img src="api/crop/…"> in the page works unchanged."""
        progress = progress or (lambda *a, **k: None)
        cache_dir = Path(cache_dir)
        index_path = cache_dir / 'samples.json'
        fp = self.fingerprint()
        if not rebuild and index_path.exists():
            try:
                cached = json.loads(index_path.read_text())
            except ValueError:
                cached = None
            if cached and cached.get('fingerprint') == fp:
                return cached
        t0 = time.time()
        progress(stage='reading the metadata table')
        summary = self.check()
        rows, cols = self.load_meta()
        meta_cols = [c['name'] for c in summary['columns']]
        types = {c['name']: c['type'] for c in summary['columns']}
        names = sorted({r.get('label') or '' for r in rows}) if 'label' in cols else ['all']
        names = [n or '(none)' for n in names]
        cls_of = {('(none)' if not n else n): i for i, n in enumerate(names)}
        samples = []
        for i, r in enumerate(rows):
            meta = {}
            for c in meta_cols:
                v = r.get(c, '')
                meta[c] = _num(v) if types[c] == 'num' else v
            preview = {c: (r[c] if c == 'text' else self._safe_ref(r[c])) for c in summary['previews'] if r.get(c)}
            cls = cls_of[(r.get('label') or '(none)')] if 'label' in cols else 0
            samples.append({'id': i, 'cls': cls, 'cls_name': names[cls],
                            'image': f'#{r.get("id") or i}', 'row_id': r.get('id') or str(i), 'session': '',
                            'meta': meta, 'preview': preview})
        stats = {'rows': len(samples), 'thumbs': 0, 'bad_previews': 0}
        if 'image' in summary['previews']:
            stats.update(self._thumbnails(samples, cache_dir / 'crops', thumb_size, progress))
        modality = sorted({k for s in samples for k in s['preview']}) or ['vector']
        result = {'fingerprint': fp, 'classes': names, 'samples': samples, 'stats': stats,
                  'kind': 'embeddings', 'meta_types': types, 'modality': modality}
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = index_path.with_name(f'{index_path.name}.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(result))
        os.replace(tmp, index_path)
        _log.info(f'indexed {len(samples):,} rows ({", ".join(modality)}) in {time.time() - t0:.1f}s')
        return result

    def _thumbnails(self, samples, crops_dir, size, progress):
        from PIL import Image, ImageOps
        crops_dir.mkdir(parents=True, exist_ok=True)
        todo = [s for s in samples if s['preview'].get('image') and not is_url(s['preview']['image'])]
        done, bad = [0], [0]

        def one(s):
            try:
                with Image.open(self.root / s['preview']['image']) as im:
                    im = ImageOps.exif_transpose(im).convert('RGB')
                    im.thumbnail((size, size))
                    buf = io.BytesIO()
                    im.save(buf, 'JPEG', quality=90)
                (crops_dir / f'{s["id"]:06d}.jpg').write_bytes(buf.getvalue())
            except Exception:  # noqa: BLE001 - one unreadable preview must not stop the index
                bad[0] += 1
            done[0] += 1
            if done[0] % 200 == 0:
                progress(done[0], len(todo), 'image previews → thumbnails')

        with ThreadPoolExecutor(8, thread_name_prefix='thumbs') as ex:
            list(ex.map(one, todo))
        return {'thumbs': len(todo) - bad[0], 'bad_previews': bad[0]}
