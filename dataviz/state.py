"""
State: everything the server knows about the dataset it is serving right now.

The dataset index plus lazily computed feature spaces, k-NN scores, projections and group bubbles,
each cached in memory (small LRUs) and in the workspace folder on disk. Slow work is queued on the
JobManager; CPU-heavy pieces run in the worker processes (core.workers).
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
from .workspace import resolve_workspace, find_workspace, _now

# Defaults; the real values come from config.yaml in this folder (State.cfg).
PALETTE = vconfig.DEFAULTS['ui']['palette']
KNN_K = vconfig.DEFAULTS['analysis']['knn_k']
PARAM_TYPES = {'n_neighbors': int, 'min_dist': float, 'perplexity': float}


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
        self.registry = registry
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
        self.index_job = self.jobs.submit(f'index:{self.tag}', f'Open dataset · {self.source.name}', self._index_job,
                                          lane='io')
        if auto_compute:
            self.request_projection(self.default_feature, self.startup_method, {}, None)
        for key in precompute:             # an explicit --precompute is a request, so it runs either way
            if key in self.registry:
                self.request_features(key)

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

    def request_features(self, key):
        spec = self.registry[key]
        return self.jobs.submit(f'features:{self.tag}:{key}', f'Features · {spec.label}',
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
        return self.jobs.submit(f'text:{self.tag}:{key}:{query}', f'Text search · {spec.label}',
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
        return self.jobs.submit(f'scores:{self.tag}:{key}', f'Scores · {spec.label}', lambda job: bool(self.get_scores(key, job)))

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

        return None, key, self.jobs.submit(f'proj:{self.tag}:{key}', label, run)

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
        backups, the ignore list and the filter / presets live here. Without a dataset id: the workspace folder."""
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

    def load_settings(self):
        """ui_settings.json of this index version, with the per-dataset keys (PERSIST_KEYS) from persist_dir on top
        (a workspace folder from before they moved still provides them until the next save)."""
        out = self._read_json(self.settings_path) if self.cache_dir is not None else {}
        out.update(self._read_json(self._persist_settings_path))
        return out

    def save_settings(self, settings):
        pp = self._persist_settings_path
        if pp is not None:
            self._write_json(pp, {k: settings[k] for k in PERSIST_KEYS if k in settings})
            settings = {k: v for k, v in settings.items() if k not in PERSIST_KEYS}
        if self.cache_dir is not None:
            self._write_json(self.settings_path, settings)

    # ------------------------------------------------------------ tags
    @property
    def tags_path(self):
        return self.cache_dir / 'tags.json'

    def load_tags(self):
        return self._read_json(self.tags_path)

    def save_tags(self, tags):
        self._write_json(self.tags_path, tags)
