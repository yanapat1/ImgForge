"""
State: everything the server knows about the dataset it is serving right now.

The dataset index plus lazily computed feature spaces, k-NN scores, projections and group bubbles,
each cached in memory (small LRUs) and in the workspace folder on disk. Slow work is queued on the
JobManager; CPU-heavy pieces run in the worker processes (core.workers).

One State per loaded visualizer session (server/app.py Ctx.states, shared by every user who opens it). What
belongs to a person or to the session lives in PostgreSQL when there is a database and a session id: the UI
settings + filter per user (app.user_session_prefs), the curation ignore list per session (app.dataset_state),
box tags per dataset version (app.box_tags). Without one (scripts, the file-only tests) the old JSON files in the
workspace folder are used.
"""

import os
import re
import json
import time
import uuid
import asyncio
import hashlib
import threading
from pathlib import Path
from collections import OrderedDict, defaultdict

import numpy as np
import orjson

from .core import config as vconfig
from .core import logging as vlog
from .core import workers as compute
from .core.jobs import JobManager
from .data import sources
from .data.indexer import build_index
from .ml import analysis
from .ml import features as feats
from .ml import label_quality as lqual
from .workspace import resolve_workspace, find_workspace, _now

# Defaults; the real values come from config.yaml in this folder (State.cfg).
PALETTE = vconfig.DEFAULTS['ui']['palette']
KNN_K = vconfig.DEFAULTS['analysis']['knn_k']
PARAM_TYPES = {'n_neighbors': int, 'min_dist': float, 'perplexity': float, 'gamma': float}


def normalize_params(method, params):
    if method not in analysis.METHODS:
        raise ValueError(f'unknown method {method!r}')
    out = dict(analysis.METHODS[method]['params'])
    out.update({k: v for k, v in (params or {}).items() if k in out})
    return {k: PARAM_TYPES.get(k, float)(v) for k, v in out.items()}   # same key whether value came as 30 or 30.0


def projection_key(feature, method, params, classes):
    cls = 'all' if not classes else ','.join(str(c) for c in sorted(set(classes)))
    return f'{feature}|{method}|{json.dumps(params, sort_keys=True)}|{cls}'



# ui settings kept per saved dataset (State.persist_dir), not per index version: they survive a re-index
PERSIST_KEYS = ('curation_ignored', 'filter', 'filter_presets')


class State:
    """Dataset index + lazily computed feature spaces, scores and projections.

    The workspace folder (cache_dir) may be unknown at construction: for an S3 dataset finding it
    needs the bucket listing (and in cache mode a sync), so the index job resolves it first.
    """

    def __init__(self, source, cache_dir, registry, default_feature=feats.DEFAULT_FEATURE,
                 jobs=None, rebuild=False, log=None, pools=None, cfg=None, workspace_root=None, dataset_id=None,
                 refresh=False):
        self.log = log or vlog.log_fn('app')
        self.cfg = cfg or vconfig.load_config()
        self.knn_k = int(self.cfg['analysis']['knn_k'])
        self.palette = list(self.cfg['ui']['palette']) or PALETTE
        self.palette_light = list(self.cfg['ui'].get('palette_light') or []) or self.palette
        self.source = source if hasattr(source, 'list_labels') else sources.LocalSource(source)
        self.dataset_id = dataset_id
        self.workspace_root = Path(workspace_root) if workspace_root else None
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.tag = uuid.uuid4().hex[:6]                 # job keys of this dataset never collide with the last one
        # the config's features (+ the owner's models, cli.py) + the embeddings imported into this session
        self.registry = registry if isinstance(registry, feats.FeatureRegistry) else feats.FeatureRegistry(registry)
        self.registry.add(self.imported_features)
        self._imports = None                            # (fingerprint, {key: FeatureSpec}) of features/imports.json
        self.default_feature = default_feature if default_feature in registry else next(iter(registry))
        self.jobs = jobs or JobManager()
        self.pools = pools or compute.InlinePools()      # main() passes worker-process pools
        self.rebuild = rebuild
        self.refresh = refresh                          # True: re-index now even if an older cache could be shown
        self.stale = None                               # set when the dataset changed after the cache shown was built

        self.index = None
        self.index_job = None
        self.samples, self.classes, self.labels, self.by_image = [], [], np.zeros(0, np.int64), {}
        self.fingerprint = None
        self.features = {}
        self.scores = {}
        self.label_quality = {}                         # feature -> (cache tag, per-box arrays, summary)
        self.projections = OrderedDict()
        self.groups_cache = OrderedDict()
        self._feature_locks = defaultdict(threading.Lock)
        self._rebuilt = set()
        self.tags_lock = threading.Lock()
        self.groups_lock = threading.Lock()
        self.index_lock = threading.Lock()
        self.payloads = OrderedDict()                   # serialized JSON bytes, ready to send
        self.edit_seq = 0                               # bumped by every curation fix / undo (state patched in place)
        self.deleted = None                             # bool per sample: boxes a curation fix removed (or None)
        self.pending_new = {}                           # image -> boxes the image editor added (no dot until re-index)
        self.stale_ids = set()                          # boxes the image editor moved (crop / features from before)
        self._image_names = None                        # (fingerprint, sorted names, set): image_names()
        self._cached_keys = set()

    @property
    def dataset_dir(self):
        """Local folder of the dataset (None for an S3 dataset in stream mode)."""
        return self.source.root

    @property
    def features_dir(self):
        return self.cache_dir / 'features'

    @property
    def proj_dir(self):
        return self.cache_dir / 'projections'

    @property
    def scores_dir(self):
        return self.cache_dir / 'scores'

    # ------------------------------------------------------------ jobs
    @property
    def ready(self):
        return self.index is not None

    @property
    def startup_method(self):
        method = self.cfg['startup'].get('method', 'umap')
        return method if method in analysis.METHODS else 'umap'

    def start(self, precompute=(), auto_compute=False):
        """Queue the index build; with auto_compute also the one startup projection; precomputed feature spaces always."""
        # the io lane: an S3 sync / listing never waits behind a long UMAP of the previous dataset
        self.index_job = self.submit(f'index:{self.tag}', f'Open dataset · {self.source.name}', self._index_job,
                                     lane='io')
        if auto_compute:
            self.request_projection(self.default_feature, self.startup_method, {}, None)
        for key in precompute:             # an explicit --precompute is a request, so it runs either way
            if key in self.registry:
                self.request_features(key)

    def submit(self, key, label, fn, **kw):
        """JobManager.submit, the job tied to this session (its owner: the user of the request, when there is one)."""
        return self.jobs.submit(key, label, fn, session_id=kw.pop('session_id', None) or self.dataset_id, **kw)

    def busy(self):
        """True while a job of this State is queued or running (it must not be unloaded)."""
        return self.jobs.busy(self.tag)

    def close(self):
        """Unloaded from the server (LRU eviction, session deleted / re-indexed): free what outlives a reference —
        the S3 source's thread pool; memory caches go with the object. Ctx.close_state also drops the caches
        other modules keep per State.tag (models/service.py splits, curation stores)."""
        for src in (self.source, getattr(self.source, 'local', None)):
            pool = getattr(src, 'pool', None)
            if pool is not None:
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except Exception:  # noqa: BLE001 - closing must never fail
                    pass
        self.features.clear()
        self.scores.clear()
        self.label_quality.clear()
        self.projections.clear()
        self.groups_cache.clear()
        self.payloads.clear()

    def embedding_cached(self):
        """True when the startup view (default params, all classes) and its k-NN scores are already on disk."""
        if not self.ready:
            return False
        feature, method = self.default_feature, self.startup_method
        key = projection_key(feature, method, normalize_params(method, {}), None)
        return ((key in self.projections or self._projection_file(key).exists())
                and (feature in self.scores or self._scores_file(feature).exists()))

    def wait_index(self, job=None):
        if self.ready:
            return
        if job:
            job.progress(stage='waiting for the dataset index')
        if self.index_job is not None:
            self.index_job.wait()
        self._require_index()

    def _index_job(self, job):
        src = self.source
        if src.kind == 's3':
            job.progress(stage=f'connecting to {src.uri}')
            src.check()
            if src.mode == 'cache':
                src.sync(job.progress, refresh=False)
        elif src.kind == 'labeling':
            job.progress(stage='reading the labeling session')
            src.check()                              # the session / task may have gone since the page opened it
        elif src.kind == 'embeddings':
            job.progress(stage='checking the vectors and metadata table')
            src.check()
        idx = None
        if self.cache_dir is None:
            job.progress(stage='checking dataset fingerprint')
            idx = self._open_last_cache(src)
            if idx is None:
                self.cache_dir = resolve_workspace(self.workspace_root, src)
        if idx is None:
            idx = self._build_index(job, src)
        return self._finish_index(job, src, idx)

    def _open_last_cache(self, src):
        """Clicking a session should not re-index (and recompute features) just because a file was touched: when the
        files changed since the last cache was built, show that cache straight away and set `self.stale` so the page
        offers "Update cache". Returns the cached index, or None (nothing cached yet / refresh asked / not a local folder)."""
        if (src.kind != 'local' or self.rebuild or self.refresh or not self.workspace_root
                or not self.cfg['dataset'].get('reuse_cache', True)):
            return None
        fp = src.fingerprint()
        if (self.workspace_root / f'{src.name}__{fp[:8]}' / 'samples.json').exists():
            return None                                  # up to date: the normal path is a cache hit
        found = find_workspace(self.workspace_root, src.uri)
        if not found or not (found[0] / 'samples.json').exists():
            return None
        try:
            idx = json.loads((found[0] / 'samples.json').read_text())
        except (OSError, ValueError):
            return None
        self.cache_dir = found[0]
        self.stale = {'cached': idx.get('fingerprint'), 'current': fp, 'built': found[1].get('last_opened'),
                      'n_boxes': len(idx.get('samples') or [])}
        self.log(f'dataset changed since the cache was built ({self.stale["built"]}): showing the cache, '
                 f'"Update cache" in the page re-indexes')
        return idx

    def _build_index(self, job, src):
        if src.kind == 'embeddings':
            return src.build_index(self.cache_dir, thumb_size=int(self.cfg['index']['thumb_size']),
                                   rebuild=self.rebuild, progress=job.progress)
        return build_index(src, self.cache_dir, thumb_size=int(self.cfg['index']['thumb_size']),
                          pad=float(self.cfg['index']['crop_pad']), rebuild=self.rebuild,
                          progress=job.progress, workers=self.cfg['index'].get('workers', 'auto'))

    def _finish_index(self, job, src, idx):
        self.samples = idx['samples']
        self.classes = idx['classes']
        self.fingerprint = idx['fingerprint']
        self.labels = np.array([s['cls'] for s in self.samples], dtype=np.int64)
        self.by_image = {}
        for s in self.samples:
            self.by_image.setdefault(s['image'], []).append(s['id'])
        meta_path = self.cache_dir / 'workspace.json'
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {'created': _now()}
        meta.update({'dataset': src.uri, 'fingerprint': self.fingerprint, 'n_boxes': len(self.samples),
                     'n_images': len(self.by_image), 'classes': self.classes, 'last_opened': _now()})
        if src.kind == 'embeddings':
            meta['modality'] = idx.get('modality')
        meta_path.write_text(json.dumps(meta, indent=1))
        self.index = idx
        self._register_existing_projections()
        return len(self.samples)

    def _require_index(self):
        if not self.ready:
            raise RuntimeError('dataset index is not ready')

    def crop_paths(self):
        return [self.cache_dir / 'crops' / f'{s["id"]:06d}.jpg' for s in self.samples]

    def feature_cached(self, key):
        if key in self.features or key in self._cached_keys:
            return True
        if self.ready and feats.is_cached(self.features_dir, key, self.fingerprint, len(self.samples)):
            self._cached_keys.add(key)
            return True
        return False

    def get_features(self, key, job=None):
        """Feature matrix for `key`: memory -> disk cache -> compute (only when job is given)."""
        if job is not None:
            self.wait_index(job)
        self._require_index()
        if key not in self.registry:
            raise KeyError(key)
        with self._feature_locks[key]:
            if key in self.features:
                return self.features[key]
            emb = None
            if not (self.rebuild and key not in self._rebuilt):
                emb = feats.load_cached(self.features_dir, key, self.fingerprint, len(self.samples))
                if emb is not None:
                    feats.vectors_backfill(self.fingerprint, key, emb, self.cfg)     # computed before PostgreSQL
                else:
                    emb = feats.vectors_restore(self.features_dir, key, self.fingerprint, len(self.samples), self.cfg)
                if emb is None:
                    emb = self._legacy_embeddings(key)
            if emb is None:
                if job is None:
                    return None
                t0 = time.time()
                emb = self.registry[key].fn(self.crop_paths(), self.samples, job.progress)
                vlog.get_logger('features').info(f'{key}: {emb.shape[0]:,} × {emb.shape[1] if emb.ndim == 2 else 0} '
                                                 f'in {time.time() - t0:.1f}s')
                feats.save_cached(self.features_dir, key, self.fingerprint, emb)
                feats.vectors_store(self.fingerprint, key, emb, self.cfg)
                self._rebuilt.add(key)
            self.features[key] = emb
            return emb

    def _legacy_embeddings(self, key):
        """Reuse embeddings.npy written by the first version of this tool (DINOv2-base)."""
        if key != 'dinov2-base':
            return None
        npy, meta = self.cache_dir / 'embeddings.npy', self.cache_dir / 'embeddings.json'
        if npy.exists() and meta.exists():
            m = json.loads(meta.read_text())
            if (m.get('fingerprint') == self.fingerprint and m.get('model') == 'facebook/dinov2-base'
                    and m.get('n') == len(self.samples)):
                emb = np.load(npy)
                feats.save_cached(self.features_dir, key, self.fingerprint, emb)
                return emb
        return None

    # ------------------------------------------------------------ imported embeddings (a file, one row per box)
    IMPORT_MAX_BYTES = 2 << 30

    def _imports_path(self):
        return self.features_dir / 'imports.json'

    def imported_features(self):
        """{key: FeatureSpec} of the embeddings imported into this dataset version (features/imports.json)."""
        if not self.ready or self.cache_dir is None:
            return {}
        if self._imports is not None and self._imports[0] == self.fingerprint:
            return self._imports[1]
        try:
            entries = json.loads(self._imports_path().read_text())
        except (OSError, ValueError):
            entries = []
        specs = {}
        for e in entries:
            if e.get('fingerprint') != self.fingerprint:
                continue

            def missing(*_, name=e['name'], **__):
                raise RuntimeError(f'the imported embeddings {name!r} are gone from the cache: import the file again')
            specs[e['key']] = feats.FeatureSpec(e['key'], f'{e["name"]} · imported', 'imported', missing, metric='cosine',
                                                note=f'imported {e["dim"]}-dimensional embeddings ({e["created"]})')
        self._imports = (self.fingerprint, specs)
        return specs

    def import_features(self, name, data):
        """Store embeddings for every box of this dataset as a feature space; returns its key.

        data: an (N, D) array in box id order (sample ids, the `id` column of the CSV export), or a dict with `v`
        (M, D) plus `id` (M,) box ids or `image` + `box_idx` (M,) to place each row (every box needs a row)."""
        self._require_index()
        n = len(self.samples)
        if isinstance(data, dict):
            v = np.asarray(data.get('v'))
            if v.ndim != 2:
                raise ValueError('the file needs an array "v" of shape (rows, dims)')
            if 'id' in data:
                ids = np.asarray(data['id']).astype(np.int64).ravel()
            elif 'image' in data and 'box_idx' in data:
                where = {(s['image'], s['box_idx']): s['id'] for s in self.samples}
                imgs, bix = np.asarray(data['image']).astype(str).ravel(), np.asarray(data['box_idx']).astype(np.int64).ravel()
                ids = np.asarray([where.get((i, int(b)), -1) for i, b in zip(imgs, bix)], np.int64)
            else:
                raise ValueError('with "v", also give "id" (box ids) or "image" + "box_idx" for every row')
            if len(ids) != len(v):
                raise ValueError(f'{len(v)} rows in "v" but {len(ids)} box references')
            if (ids < 0).any() or (ids >= n).any():
                raise ValueError(f'{int(((ids < 0) | (ids >= n)).sum())} rows point at no box of this dataset')
            emb = np.full((n, v.shape[1]), np.nan, np.float32)
            emb[ids] = v
        else:
            emb = np.asarray(data, np.float32)
            if emb.ndim != 2 or len(emb) != n:
                raise ValueError(f'expected {n:,} rows (one per box, in box id order), got shape {tuple(emb.shape)}')
        if emb.shape[1] < 2 or emb.shape[1] > 16384:
            raise ValueError(f'{emb.shape[1]} dimensions: expected 2 to 16384')
        bad = ~np.isfinite(emb).all(1)
        if bad.any():
            raise ValueError(f'{int(bad.sum()):,} boxes have no row or a NaN / inf value')
        emb = feats.l2_normalize(emb)
        slug = re.sub(r'[^A-Za-z0-9_-]+', '-', str(name or 'embeddings')).strip('-')[:40] or 'embeddings'
        key = f'import:{slug}:{hashlib.sha1(emb.tobytes()).hexdigest()[:8]}'
        self.features_dir.mkdir(parents=True, exist_ok=True)
        feats.save_cached(self.features_dir, key, self.fingerprint, emb)
        feats.vectors_store(self.fingerprint, key, emb, self.cfg)
        try:
            entries = json.loads(self._imports_path().read_text())
        except (OSError, ValueError):
            entries = []
        entries = [e for e in entries if e.get('key') != key]
        entries.append({'key': key, 'name': str(name or slug)[:80], 'dim': int(emb.shape[1]), 'n': n,
                        'fingerprint': self.fingerprint, 'created': _now()})
        self._write_json(self._imports_path(), entries)
        self._imports = None
        self.features.pop(key, None)
        return key

    def remove_imported(self, key):
        try:
            entries = json.loads(self._imports_path().read_text())
        except (OSError, ValueError):
            entries = []
        if not any(e.get('key') == key for e in entries):
            raise KeyError(key)
        self._write_json(self._imports_path(), [e for e in entries if e.get('key') != key])
        self._imports = None
        self.features.pop(key, None)
        self.scores.pop(key, None)
        self.label_quality.pop(key, None)

    def request_features(self, key):
        spec = self.registry[key]
        return self.submit(f'features:{self.tag}:{key}', f'Features · {spec.label}',
                                lambda job: self.get_features(key, job).shape)

    # ------------------------------------------------------------ text search
    def text_ready(self, key, query):
        spec = self.registry[key]
        return feats.TEXT.has(spec.text_model, query) and (key in self.features or self.feature_cached(key))

    def text_search(self, key, query, k, job=None):
        """Boxes whose crop embedding is closest to the text `query` (cosine): {'id': [...], 'score': [...]}."""
        spec = self.registry[key]
        emb = self.get_features(key, job)
        if emb is None:
            return None
        t = feats.TEXT.encode(spec.text_model, spec.kind, query, job.progress if job else None)
        sim = emb @ t
        dm = self.deleted_mask()
        if dm is not None:                              # boxes a curation fix removed are not results
            sim = np.where(dm, -np.inf, sim)
            k = min(k, int(len(sim) - dm.sum()))
            if k <= 0:
                return {'id': [], 'score': np.zeros(0, np.float32)}
        k = max(1, min(k, len(sim)))
        top = np.argpartition(-sim, k - 1)[:k]
        top = top[np.argsort(-sim[top])]
        return {'id': top.tolist(), 'score': sim[top]}

    def request_text_search(self, key, query, k):
        spec = self.registry[key]
        return self.submit(f'text:{self.tag}:{key}:{query}', f'Text search · {spec.label}',
                                lambda job: bool(self.text_search(key, query, k, job)))

    def _scores_file(self, key):
        return self.scores_dir / f'{feats.cache_name(key)}_{self.fingerprint[:8]}.npz'

    def get_scores(self, key, job=None):
        if job is not None:
            self.wait_index(job)
        if key in self.scores:
            return self.scores[key]
        path = self._scores_file(key) if self.ready else None
        if path is not None and path.exists() and not (self.rebuild and key not in self._rebuilt):
            z = np.load(path)
            self.scores[key] = {k: z[k] for k in ('nbr_idx', 'nbr_sim', 'suspicion', 'suggested', 'outlier')}
            return self.scores[key]
        emb = self.get_features(key, job)
        if emb is None:
            return None
        if job:
            job.progress(stage='k-NN + label scores')
        self.scores[key] = self.pools.run('heavy', compute.do_scores, emb, self.labels, self.registry[key].metric, self.knn_k,
                                          progress=job.progress if job else None)
        self.scores_dir.mkdir(exist_ok=True)
        np.savez(self._scores_file(key), **self.scores[key])
        return self.scores[key]

    def request_scores(self, key):
        spec = self.registry[key]
        return self.submit(f'scores:{self.tag}:{key}', f'Scores · {spec.label}', lambda job: bool(self.get_scores(key, job)))

    # ------------------------------------------------------------ label quality (cleanlab, k-fold pred_probs)
    def label_quality_settings(self, **override):
        return lqual.settings(self.cfg['analysis'].get('label_quality'), **override)

    def label_quality_tag(self, settings):
        """Cache tag: settings + the current labels and removed boxes (a curation relabel recomputes)."""
        dm = self.deleted_mask()
        h = hashlib.sha1(np.ascontiguousarray(self.labels).tobytes())
        if dm is not None:
            h.update(np.packbits(dm).tobytes())
        return f'{lqual.settings_hash(settings)}{h.hexdigest()[:8]}'

    def _lq_file(self, key, tag):
        return self.scores_dir / f'lq_{feats.cache_name(key)}_{self.fingerprint[:8]}_{tag}.npz'

    def cached_label_quality(self, key, **override):
        """(per-box arrays, summary) when computed for the current labels and settings, else None (memory, then disk)."""
        if not self.ready:
            return None
        s = self.label_quality_settings(**override)
        tag = self.label_quality_tag(s)
        hit = self.label_quality.get(key)
        if hit is not None and hit[0] == tag:
            return hit[1], hit[2]
        path = self._lq_file(key, tag)
        if not path.exists():
            return None
        z = np.load(path)
        per = {k: z[k] for k in lqual.PER_BOX if k in z}
        summary = json.loads(path.with_suffix('.json').read_text())
        self.label_quality[key] = (tag, per, summary)
        return per, summary

    def get_label_quality(self, key, job=None, **override):
        if job is not None:
            self.wait_index(job)
        hit = self.cached_label_quality(key, **override)
        if hit is not None:
            return hit
        emb = self.get_features(key, job)
        if emb is None:
            return None
        s = self.label_quality_settings(**override)
        tag = self.label_quality_tag(s)
        dm = self.deleted_mask()
        if job:
            job.progress(stage='label quality: k-fold predicted probabilities')
        sc = self.get_scores(key, job)                  # its k-NN gives the out-of-distribution score for free
        per, summary = self.pools.run('heavy', compute.do_label_quality, emb, self.labels, s, list(self.classes),
                                      None if dm is None else ~dm, None if sc is None else sc['nbr_sim'],
                                      progress=job.progress if job else None)
        summary['feature'] = key
        self.scores_dir.mkdir(exist_ok=True)
        path = self._lq_file(key, tag)
        np.savez(path, **per)
        path.with_suffix('.json').write_text(json.dumps(summary))
        self.label_quality[key] = (tag, per, summary)
        return per, summary

    def request_label_quality(self, key, **override):
        spec = self.registry[key]
        tag = self.label_quality_tag(self.label_quality_settings(**override))
        return self.submit(f'lq:{self.tag}:{key}:{tag}', f'Label quality · {spec.label}',
                           lambda job: bool(self.get_label_quality(key, job, **override)))

    def _projection_file(self, key):
        h = hashlib.sha1(f'{key}|{self.fingerprint}'.encode()).hexdigest()[:16]
        return self.proj_dir / f'{h}.npz'

    @property
    def _index_path(self):
        return self.proj_dir / 'index.json'

    def saved_projections(self):
        if not self._index_path.exists():
            return {}
        try:
            return json.loads(self._index_path.read_text())
        except ValueError:
            return {}

    def _index_update(self, key, **fields):
        with self.index_lock:
            idx = self.saved_projections()
            entry = idx.get(key)
            if entry is None:
                feature, method, params, cls = key.split('|', 3)
                entry = {'key': key, 'feature': feature, 'method': method, 'params': json.loads(params),
                         'classes': None if cls == 'all' else [int(c) for c in cls.split(',')],
                         'created': _now()}
            entry.update(fields)
            idx[key] = entry
            self.proj_dir.mkdir(exist_ok=True)
            tmp = self._index_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(idx, indent=1))
            tmp.replace(self._index_path)

    def _register_existing_projections(self):
        """Add projections saved by earlier versions (all classes, default params) to index.json."""
        known = self.saved_projections()
        for fkey in self.registry:
            for method in analysis.METHODS:
                key = projection_key(fkey, method, normalize_params(method, {}), None)
                if key not in known and self._projection_file(key).exists():
                    z = np.load(self._projection_file(key))
                    self._index_update(key, n=int(len(z['id'])), created='earlier')

    def get_projection(self, key):
        """Cached projection dict or None (memory, then the workspace folder on disk)."""
        if key in self.projections:
            self.projections.move_to_end(key)
            return self.projections[key]
        if self.ready and not (self.rebuild and key not in self._rebuilt):
            path = self._projection_file(key)
            if path.exists():
                z = np.load(path)
                proj = {'id': z['id'], 'xy': z['xy'], 'info': json.loads(str(z['info']))}
                self._store_projection(key, proj)
                return proj
        return None

    def _store_projection(self, key, proj):
        self.projections[key] = proj
        while len(self.projections) > 48:
            self.projections.popitem(last=False)

    def request_projection(self, feature, method, params, classes):
        """Returns (projection, None) if cached, else (None, job)."""
        if feature not in self.registry:
            raise KeyError(feature)
        params = normalize_params(method, params)
        key = projection_key(feature, method, params, classes)
        proj = self.get_projection(key) if self.ready else None
        if proj is not None and (feature in self.scores or self._scores_file(feature).exists()):
            self.get_scores(feature)
            self._index_update(key, last_used=_now())
            return proj, key, None
        label = f'{analysis.METHODS[method]["label"]} · {self.registry[feature].label}'
        if classes:
            label += f' · {len(set(classes))} class(es)'

        def run(job):
            self.wait_index(job)
            self.get_scores(feature, job)          # computes the features too if needed
            if self.get_projection(key) is not None:
                return key
            emb = self.get_features(feature, job)   # scores may have come from disk without the features
            ids = np.arange(len(self.samples)) if not classes else np.nonzero(np.isin(self.labels, list(classes)))[0]
            t0 = time.time()
            xy, info = self.pools.run('heavy', compute.do_project, emb[ids], method, params, self.registry[feature].metric,
                                      progress=job.progress)
            proj = {'id': ids, 'xy': xy, 'info': info}
            self._store_projection(key, proj)
            self._rebuilt.add(key)
            self.proj_dir.mkdir(exist_ok=True)
            np.savez(self._projection_file(key), id=ids, xy=xy, info=json.dumps(info))
            self._index_update(key, n=int(len(ids)), compute_s=round(time.time() - t0, 1), last_used=_now())
            return key

        return None, key, self.submit(f'proj:{self.tag}:{key}', label, run)

    def _groups_store(self, gkey, res):
        with self.groups_lock:
            self.groups_cache[gkey] = res
            while len(self.groups_cache) > 64:
                self.groups_cache.popitem(last=False)
        return res

    def image_names(self):
        """Every image of the source, background images (no boxes) included, sorted; listed once per index."""
        cur = self._image_names
        if cur is None or cur[0] != self.fingerprint:
            names = sorted(self.source.list_images())
            cur = self._image_names = (self.fingerprint, names, set(names))
        return cur[1]

    def has_image(self, name):
        self.image_names()
        return name in self._image_names[2]

    def deleted_mask(self):
        """Bool per sample of the boxes curation fixes removed since the index was built, None when there are none."""
        d = self.deleted
        if d is None or len(d) != len(self.samples) or not d.any():
            return None
        return d

    def _groups_input(self, proj):
        """(xy, labels, keep) for the density groups: boxes a curation fix removed are left out (keep=None: all)."""
        ids = proj['id']
        dm = self.deleted_mask()
        keep = None if dm is None else ~dm[ids]
        if keep is not None and keep.all():
            keep = None
        if keep is None:
            return proj['xy'], self.labels[ids], None
        return proj['xy'][keep], self.labels[ids[keep]], keep

    @staticmethod
    def _groups_expand(res, keep):
        """Groups of the kept points back to one entry per projected point (removed boxes: -2, not outliers)."""
        if keep is None:
            return res
        group, groups = res
        full = np.full(len(keep), -2, np.asarray(group).dtype)
        full[keep] = group
        return full, groups

    def groups(self, key, min_cluster_size, per_class):
        proj = self.get_projection(key)
        if proj is None:
            return None
        gkey = (key, int(min_cluster_size), bool(per_class), self.edit_seq)
        if gkey in self.groups_cache:
            return self.groups_cache[gkey]
        xy, lab, keep = self._groups_input(proj)
        res = self.pools.run('fast', compute.do_groups, xy, lab, int(min_cluster_size),
                             bool(per_class), float(self.cfg['analysis']['groups']['bubble_pad']))
        return self._groups_store(gkey, self._groups_expand(res, keep))

    async def groups_async(self, key, min_cluster_size, per_class):
        """Same as groups(), but awaits the worker process so the event loop stays free."""
        proj = self.get_projection(key)
        if proj is None:
            return None
        gkey = (key, int(min_cluster_size), bool(per_class), self.edit_seq)
        if gkey in self.groups_cache:
            return self.groups_cache[gkey]
        xy, lab, keep = self._groups_input(proj)
        fut = self.pools.submit('fast', compute.do_groups, xy, lab, int(min_cluster_size),
                                bool(per_class), float(self.cfg['analysis']['groups']['bubble_pad']))
        return self._groups_store(gkey, self._groups_expand(await asyncio.wrap_future(fut), keep))

    def payload(self, key, build):
        """Serialized JSON bytes for `key`, built once (orjson, numpy arrays serialized natively)."""
        data = self.payloads.get(key)
        if data is None:
            data = orjson.dumps(build(), option=orjson.OPT_SERIALIZE_NUMPY)
            self.payloads[key] = data
            while len(self.payloads) > 64:
                self.payloads.popitem(last=False)
        else:
            self.payloads.move_to_end(key)
        return data

    # ------------------------------------------------------------ settings
    @property
    def persist_dir(self):
        """Per saved dataset (<workspace root>/curation/<dataset id>/), not per index version like cache_dir:
        a curation fix changes the fingerprint, so after a re-index the workspace folder is a new one. Curation
        backups (and, without a database, the ignore list and the filter / presets) live here. Without a dataset
        id: the workspace folder."""
        if self.workspace_root is not None and self.dataset_id:
            return self.workspace_root / 'curation' / re.sub(r'[^\w.-]', '_', str(self.dataset_id))
        return self.cache_dir

    @property
    def settings_path(self):
        return self.cache_dir / 'ui_settings.json'

    @property
    def _persist_settings_path(self):
        d = self.persist_dir
        return None if d is None or d == self.cache_dir else d / 'settings.json'

    @staticmethod
    def _read_json(path):
        try:
            return json.loads(path.read_text()) if path is not None and path.exists() else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write_json(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(path)

    def _db_session(self, user_id=None, need_user=True):
        """The database is the store when there is one and this State is a saved session (and, for per-user rows,
        a user): the session id, else None (the JSON files)."""
        if not self.dataset_id or (need_user and not user_id) or not _db_on():
            return None
        return self.dataset_id

    def load_settings(self, user_id=None):
        """The page's settings: the UI (per user), the filter + presets (per user) and the curation ignore list (per
        session). Files: ui_settings.json of this index version with the PERSIST_KEYS from persist_dir on top."""
        sid = self._db_session(user_id)
        if sid is None:
            out = self._read_json(self.settings_path) if self.cache_dir is not None else {}
            out.update(self._read_json(self._persist_settings_path))
            return out
        from sqlalchemy import text
        from . import db
        with db.connect() as c:
            prefs = c.execute(text('SELECT data FROM app.user_session_prefs WHERE user_id = :u AND session_id = :s'),
                              {'u': user_id, 's': sid}).scalar() or {}
            shared = c.execute(text('SELECT data FROM app.dataset_state WHERE session_id = :s'), {'s': sid}).scalar() or {}
        out = dict(prefs.get('ui') or {})
        out.update({k: prefs[k] for k in ('filter', 'filter_presets') if k in prefs})
        if 'curation_ignored' in shared:
            out['curation_ignored'] = shared['curation_ignored']
        return out

    def save_settings(self, settings, user_id=None):
        sid = self._db_session(user_id)
        if sid is None:
            pp = self._persist_settings_path
            if pp is not None:
                self._write_json(pp, {k: settings[k] for k in PERSIST_KEYS if k in settings})
                settings = {k: v for k, v in settings.items() if k not in PERSIST_KEYS}
            if self.cache_dir is not None:
                self._write_json(self.settings_path, settings)
            return
        from sqlalchemy import text
        from . import db
        mine = {k: settings[k] for k in ('filter', 'filter_presets') if k in settings}
        mine['ui'] = {k: v for k, v in settings.items() if k not in PERSIST_KEYS}
        with db.begin() as c:
            # the whole UI dict is replaced (the page sends all of it); other keys (label page position, ...) stay
            c.execute(text("""INSERT INTO app.user_session_prefs(user_id, session_id, data)
                              VALUES (:u, :s, CAST(:d AS jsonb))
                              ON CONFLICT (user_id, session_id) DO UPDATE
                              SET data = app.user_session_prefs.data || EXCLUDED.data, updated_at = now()"""),
                      {'u': user_id, 's': sid, 'd': json.dumps(mine)})
        # curation_ignored is shared by the session: changed only through update_ignored(), never overwritten here

    def ignored_ids(self):
        """Curation issues marked "ignore": shared by everyone with access to the session."""
        sid = self._db_session(need_user=False)
        if sid is None:
            return self.load_settings().get('curation_ignored', [])
        from sqlalchemy import text
        from . import db
        with db.connect() as c:
            d = c.execute(text('SELECT data FROM app.dataset_state WHERE session_id = :s'), {'s': sid}).scalar() or {}
        return list(d.get('curation_ignored') or [])

    def update_ignored(self, ids, ignore=True):
        """Add (ignore=True) or remove issue ids from the ignore list; returns the new list."""
        def change(cur):
            cur = list(dict.fromkeys(cur or []))
            if ignore:
                have = set(cur)
                return cur + [i for i in ids if i not in have]
            drop = set(ids)
            return [i for i in cur if i not in drop]
        sid = self._db_session(need_user=False)
        if sid is None:
            with self.tags_lock:                      # read-modify-write: one writer at a time
                settings = self.load_settings()
                settings['curation_ignored'] = out = change(settings.get('curation_ignored'))
                self.save_settings(settings)
            return out
        from sqlalchemy import text
        from . import db
        with db.begin() as c:
            c.execute(text("""INSERT INTO app.dataset_state(session_id) VALUES (:s) ON CONFLICT DO NOTHING"""), {'s': sid})
            d = c.execute(text('SELECT data FROM app.dataset_state WHERE session_id = :s FOR UPDATE'),
                          {'s': sid}).scalar() or {}
            out = change(d.get('curation_ignored'))
            c.execute(text("""UPDATE app.dataset_state SET data = data || jsonb_build_object('curation_ignored',
                              CAST(:v AS jsonb)), updated_at = now() WHERE session_id = :s"""),
                      {'s': sid, 'v': json.dumps(out)})
        return out

    # ------------------------------------------------------------ tags
    @property
    def tags_path(self):
        return self.cache_dir / 'tags.json'

    def _tags_db(self):
        return bool(self.fingerprint) and _db_on()

    def load_tags(self):
        """{tag: [sample ids]} of this dataset version (app.box_tags, keyed by the fingerprint; else tags.json)."""
        if not self._tags_db():
            return self._read_json(self.tags_path)
        from sqlalchemy import text
        from . import db
        with db.connect() as c:
            rows = c.execute(text('SELECT tag, sample_ids FROM app.box_tags WHERE fingerprint = :f ORDER BY tag'),
                             {'f': self.fingerprint}).all()
        return {r.tag: list(r.sample_ids) for r in rows}

    def save_tags(self, tags):
        """Replace every tag of this dataset version with `tags`."""
        if not self._tags_db():
            return self._write_json(self.tags_path, tags)
        from sqlalchemy import text
        from . import db
        with db.begin() as c:
            c.execute(text('DELETE FROM app.box_tags WHERE fingerprint = :f'), {'f': self.fingerprint})
            rows = [{'f': self.fingerprint, 't': t, 'ids': sorted(int(i) for i in ids)} for t, ids in tags.items() if ids]
            if rows:
                c.execute(text('INSERT INTO app.box_tags(fingerprint, tag, sample_ids) VALUES (:f, :t, :ids)'), rows)

    def update_tag(self, tag, ids, remove=False):
        """Add / remove sample ids to one tag atomically (an empty tag is dropped); returns all tags."""
        if not self._tags_db():
            with self.tags_lock:
                tags = self.load_tags()
                cur = set(tags.get(tag, []))
                cur = cur - set(ids) if remove else cur | set(ids)
                if cur:
                    tags[tag] = sorted(cur)
                else:
                    tags.pop(tag, None)
                self.save_tags(tags)
            return tags
        from sqlalchemy import text
        from . import db
        with db.begin() as c:
            c.execute(text("""INSERT INTO app.box_tags(fingerprint, tag) VALUES (:f, :t) ON CONFLICT DO NOTHING"""),
                      {'f': self.fingerprint, 't': tag})
            cur = set(c.execute(text('SELECT sample_ids FROM app.box_tags WHERE fingerprint = :f AND tag = :t FOR UPDATE'),
                                {'f': self.fingerprint, 't': tag}).scalar() or [])
            cur = cur - set(ids) if remove else cur | set(ids)
            if cur:
                c.execute(text('UPDATE app.box_tags SET sample_ids = :ids WHERE fingerprint = :f AND tag = :t'),
                          {'f': self.fingerprint, 't': tag, 'ids': sorted(int(i) for i in cur)})
            else:
                c.execute(text('DELETE FROM app.box_tags WHERE fingerprint = :f AND tag = :t'),
                          {'f': self.fingerprint, 't': tag})
        return self.load_tags()


def _db_on():
    """True when the server has a database (dataviz.db initialised)."""
    try:
        from . import db
        db.engine()
    except RuntimeError:
        return False
    return True
