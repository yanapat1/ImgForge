"""
Settings for the dataset visualizer, read from config.yaml next to this file.

Precedence: command-line flags > the YAML file > DEFAULTS below. Keys missing from the
file fall back to DEFAULTS, so an old or partial config keeps working. Unknown keys are
reported (usually a typo) and ignored.
"""

import copy
import os
import re
from pathlib import Path

import yaml

from ..paths import PROJECT_ROOT, DEFAULT_CONFIG  # noqa: F401  (re-exported: config.PROJECT_ROOT)

IMAGENET = {'mean': [0.485, 0.456, 0.406], 'std': [0.229, 0.224, 0.225]}

DEFAULTS = {
    'server': {'host': '::', 'port': 5151},
    'dataset': {'path': None, 'workspace_root': '.dataviz_cache', 'workspace': None, 'rebuild': False,
                'reuse_cache': True},   # files changed since the cache was built: show the cache + an "Update cache" button
    'hardware': {'device': 'auto', 'precision': 'auto'},   # auto = GPU if present and healthy, else CPU; DATAVIZ_DEVICE overrides
    'index': {'thumb_size': 224, 'crop_pad': 0.1, 'workers': 'auto', 'reuse_previous': True},
    'startup': {'feature': 'openclip-h14', 'method': 'umap', 'precompute': [],
                'auto_compute': False},   # False: features + projection run only when the user clicks "Compute embedding"
    'features': {
        'batch_size': 128,
        'models': [
            {'key': 'dinov2-base', 'label': 'DINOv2 base (768-d)', 'type': 'dinov2', 'model': 'facebook/dinov2-base',
             'note': 'general visual similarity, default'},
            {'key': 'dinov2-small', 'label': 'DINOv2 small (384-d)', 'type': 'dinov2', 'model': 'facebook/dinov2-small'},
            {'key': 'dinov2-large', 'label': 'DINOv2 large (1024-d)', 'type': 'dinov2', 'model': 'facebook/dinov2-large'},
            {'key': 'clip-b32', 'label': 'CLIP ViT-B/32 (512-d)', 'type': 'clip', 'model': 'openai/clip-vit-base-patch32',
             'note': 'semantic / concept similarity'},
            {'key': 'siglip2-so400m', 'label': 'SigLIP 2 So400m (1152-d) · new', 'type': 'siglip',
             'model': 'google/siglip2-so400m-patch14-224', 'batch_size': 64,
             'note': 'Google 2025, Apache-2.0: strongest open CLIP-style encoder'},
            {'key': 'dinov2reg-large', 'label': 'DINOv2-reg large (1024-d) · new', 'type': 'dinov2',
             'model': 'facebook/dinov2-with-registers-large', 'batch_size': 64,
             'note': 'Meta, Apache-2.0: DINOv2 + registers, cleaner fine-grained features'},
            {'key': 'openclip-h14', 'label': 'OpenCLIP ViT-H/14 (1024-d) · new', 'type': 'clip',
             'model': 'laion/CLIP-ViT-H-14-laion2B-s32B-b79K', 'batch_size': 64,
             'note': 'LAION, MIT: fully open data, best class separation on our dataset'},
        ],
        'yolo': {'auto_discover': True, 'runs_dir': 'runs', 'limit': 5, 'exclude': ['rfdetr'],
                 'extra_weights': [], 'batch_size': 64, 'imgsz': 224},
        'simple': ['color-hist', 'hog', 'geometry'],
    },
    'projections': {
        'umap': {'n_neighbors': 15, 'min_dist': 0.1},
        'pacmap': {'n_neighbors': 10},
        'localmap': {'n_neighbors': 10},
        'tsne': {'perplexity': 30},
        'pca': {},
        'kpca': {'gamma': 0},
        'isomap': {'n_neighbors': 10},
        'lle': {'n_neighbors': 10},
        'spectral': {'n_neighbors': 10},
        'tsne_threads': 8,
    },
    'analysis': {
        'knn_k': 10,
        'duplicate_threshold': 0.97,
        'duplicate_quantile': 0.005,
        'groups': {'min_cluster_size': 15, 'per_class': True, 'bubble_pad': 0.015},
        # cleanlab on k-fold out-of-fold class probabilities (Find issues › Label issues), see ml/label_quality.py
        'label_quality': {'folds': 5, 'classifier': 'logreg', 'pca_dims': 256, 'quality_method': 'self_confidence',
                          'filter_by': 'prune_by_noise_rate', 'ood': True, 'min_class_count': 5},
    },
    'models': {
        'store_dir': None,                 # models.json + uploads; None = dataset.workspace_root
        'allowed_roots': [],               # extra folders model files may live in (the project root always is)
        'split_source': 'dataset_split',   # split dataset (train/ val/) or split_report.json the models were trained on
        'default_conf': 0.25,
        'match_iou': 0.5,
        'batch_size': 16,
    },
    'workers': {'enabled': True, 'warmup': True, 'omp_threads': 'auto'},   # auto = hardware.workers('omp')
    # PostgreSQL (accounts, sessions, settings, annotations, vectors). ${VAR} / ${VAR:-default} read the environment
    'database': {'url': '${DATABASE_URL}', 'pool_size': 10},
    'auth': {'cookie_secure': '${DATAVIZ_COOKIE_SECURE:-auto}',   # auto = Secure cookie when the request came over https
             'session_idle_days': 7, 'session_max_days': 30},
    'sessions': {'max_loaded': 4},          # visualizer sessions kept loaded at once (shared by every user)
    # folders users may read datasets / models from (and browse in the page); relative = to the project root
    'storage': {'allowed_roots': ['.']},
    'embeddings': {'allow_url_previews': False},
    'vectors': {'enabled': True, 'hnsw_min_rows': 50000},   # pgvector copy of every feature set; HNSW index above n rows
    's3': {'concurrency': 32, 'connect_timeout': 5, 'read_timeout': 60, 'retries': 5},
    'logging': {'level': 'info', 'http': True, 'static': False},
    'ui': {
        'page_size': 100, 'thumb_size': 110, 'dot_size': 5, 'similar_k': 48, 'text_search_k': 200, 'check_list_size': 300,
        'palette': ['#ff6b6b', '#5fd17a', '#ffd84d', '#5aa9ff', '#ff9f43', '#b98cff', '#4fd8e0', '#ff7ad9',
                    '#c3e65a', '#ffb3b3', '#2fbfa3', '#d9b8ff', '#d4a373', '#fff1a8', '#e05780', '#9ff0c0',
                    '#c8c25a', '#ffc9a3', '#8c9eff', '#b0b6bd'],
        # same hues for the light theme, darkened to >= 4.3:1 on its plot background
        'palette_light': ['#d93636', '#23833a', '#8f7000', '#006fe9', '#b85a00', '#8e45ff', '#158087', '#db009c',
                          '#627d10', '#b03a48', '#1d8470', '#9c3fb0', '#a3682e', '#3f6e8c', '#dc285e', '#3d7a5a',
                          '#7c7727', '#8a5a44', '#4562ff', '#6c7682'],
    },
}

# sections whose keys are free-form (not checked against DEFAULTS)
_FREE_FORM = {('projections',)}


def _merge(base, override, path, unknown):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        here = path + (k,)
        if k not in base and path not in _FREE_FORM:
            unknown.append('.'.join(here))
            continue
        if isinstance(base.get(k), dict) and isinstance(v, dict):
            out[k] = _merge(base[k], v, here, unknown)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path=None, log=None):
    """DEFAULTS merged with the YAML file at `path` (default: config.yaml next to this file, if it exists)."""
    path = Path(path) if path else DEFAULT_CONFIG
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError(f'{path}: top level must be a mapping')
    elif path != DEFAULT_CONFIG:
        raise FileNotFoundError(path)
    unknown = []
    cfg = _merge(DEFAULTS, data, (), unknown)
    if unknown:
        from . import logging as vlog
        warn = vlog.get_logger('config').warning
        for key in unknown:
            (log or warn)(f'unknown key "{key}" in {path.name} (ignored — typo?)')
    cfg = expand_env(cfg)
    cfg['_path'] = str(path) if path.exists() else None
    return cfg


_ENV = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}')


def expand_env(v):
    """${VAR} / ${VAR:-default} in every string of the config (unset without a default = empty string)."""
    if isinstance(v, dict):
        return {k: expand_env(x) for k, x in v.items()}
    if isinstance(v, list):
        return [expand_env(x) for x in v]
    if isinstance(v, str) and '${' in v:
        return _ENV.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ''), v)
    return v


def resolve(p):
    """Config paths are relative to the project root."""
    p = Path(p).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p
